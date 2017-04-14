import onmt
import argparse
import numpy
import torch
import torch.nn as nn
from torch import cuda
from torch.autograd import Variable
import math
import time
from tensorboard_logger import configure, log_value



parser = argparse.ArgumentParser(description='train.py')

## Data options

parser.add_argument('-data', required=True,
                    help='Path to the *-train.pt file from preprocess.py')
parser.add_argument('-save_model', default='model',
                    help="""Model filename (the model will be saved as
                    <save_model>_epochN_PPL.pt where PPL is the
                    validation perplexity""")
parser.add_argument('-train_from',
                    help="""If training from a checkpoint then this is the
                    path to the pretrained model.""")

## Model options

parser.add_argument('-layers', type=int, default=2,
                    help='Number of layers in the LSTM encoder/decoder')
parser.add_argument('-rnn_size', type=int, default=100,
                    help='Size of LSTM hidden states')
parser.add_argument('-word_vec_size', type=int, default=100,
                    help='Word embedding sizes')
parser.add_argument('-input_feed', type=int, default=1,
                    help="""Feed the context vector at each time step as
                    additional input (via concatenation with the word
                    embeddings) to the decoder.""")
# parser.add_argument('-residual',   action="store_true",
#                     help="Add residual connections between RNN layers.")
parser.add_argument('-brnn', action='store_true',
                    help='Use a bidirectional encoder')
parser.add_argument('-brnn_merge', default='concat',
                    help="""Merge action for the bidirectional hidden states:
                    [concat|sum]""")

## Optimization options

parser.add_argument('-batch_size', type=int, default=4,
                    help='Maximum batch size')
parser.add_argument('-epochs', type=int, default=30,
                    help='Number of training epochs')
parser.add_argument('-start_epoch', type=int, default=1,
                    help='The epoch from which to start')
parser.add_argument('-param_init', type=float, default=0.1,
                help="""Parameters are initialized over uniform distribution
                    with support (-param_init, param_init)""")
parser.add_argument('-optim', default='sgd',
                    help="Optimization method. [sgd|adagrad|adadelta|adam]")
parser.add_argument('-learning_rate', type=float, default=1.0,
                    help="""Starting learning rate. If adagrad/adadelta/adam
                    is used, then this is the global learning rate.
                    Recommended settings:
                    sgd = 1, adagrad = 0.1, adadelta = 1, adam = 0.1""")
parser.add_argument('-max_grad_norm', type=float, default=5,
                    help="""If the norm of the gradient vector exceeds this,
                    renormalize it to have the norm equal to max_grad_norm""")
parser.add_argument('-dropout', type=float, default=0.3,
                    help='Dropout probability; applied between LSTM stacks.')
parser.add_argument('-learning_rate_decay', type=float, default=0.5,
                    help="""Decay learning rate by this much if (i) perplexity
                    does not decrease on the validation set or (ii) epoch has
                    gone past the start_decay_at_limit""")
parser.add_argument('-start_decay_at', default=8,
                    help="Start decay after this epoch")
parser.add_argument('-curriculum', action="store_true",
                    help="""For this many epochs, order the minibatches based
                    on source sequence length. Sometimes setting this to 1
                    will increase convergence speed.""")
parser.add_argument('-pre_word_vecs_enc',
                    help="""If a valid path is specified, then this will load
                    pretrained word embeddings on the encoder side.
                    See README for specific formatting instructions.""")
parser.add_argument('-pre_word_vecs_dec',
                    help="""If a valid path is specified, then this will load
                    pretrained word embeddings on the decoder side.
                    See README for specific formatting instructions.""")
parser.add_argument('-sample', type=int, default=1,
                    help="""Number of Samples to draw for Monte Carlo
                    approximation of loss.""")
parser.add_argument('-sample_reinforce', type=int, default=1,
                    help="""Number of Samples to draw for Monte Carlo
                    approximation of Reward for Reinforce Algorithm.""")
parser.add_argument('-max_len_latent', type=int, default=64,
                    help="""Maximum Length of the Latent Sequence.""")
parser.add_argument('-latent_vec_size', type=int, default=100,
                    help="""Dimension of Gaussian Variates of the latent
                    sequence.""")
parser.add_argument('-gamma', type=float, default=0.99,
                    help="""Decay Parameter For Geometric Prior Distribution
                    Over the Length of the Latent Sequence.""")
parser.add_argument('-lam', type=float, default=1.0,
                    help="""Balancing factor lambda for the contribution of
                    reinforcement to the total loss.""")
parser.add_argument('-logdir', default='run',
                    help="Tensorboard Logdir")



# GPU
parser.add_argument('-gpus', default=[], nargs='+', type=int,
                    help="Use CUDA")

parser.add_argument('-log_interval', type=int, default=50,
                    help="Print stats at this interval.")

# parser.add_argument('-seed', type=int, default=3435,
#                     help="Seed for random initialization")

opt = parser.parse_args()
opt.cuda = len(opt.gpus)
print(opt)
configure("runs/" + opt.logdir, flush_secs=5)
if torch.cuda.is_available() and not opt.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with -cuda")

if opt.cuda:
    cuda.set_device(opt.gpus[0])

def eval(model, criterion, data, epoch):
    total_loss = 0
    total_words = 0

    model.eval()
    criterion.eval()
    for i in range(len(data)):
        batch = [x.transpose(0, 1) for x in data[i]] # must be batch first for gather/scatter in DataParallel
        outputs = model(batch)  # FIXME volatile
        targets = batch[1][:, 1:]  # exclude <s> from targets
        _, loss_report = criterion.forward(outputs, targets, step=0)
        total_loss += loss_report
        total_words += targets.data.ne(onmt.Constants.PAD).sum()
    model.train()
    criterion.train()
    return total_loss / total_words


def trainModel(model, trainData, validData, dataset, optim):
    print(model)
    model.train()
    if optim.last_ppl is None:
        for p in model.parameters():
            p.data.uniform_(-opt.param_init, opt.param_init)
    # define criterion of each GPU
    criterion = onmt.Models.Loss(opt, model.generator,
                            dataset['dicts']['tgt'].size())
    if opt.cuda:
        criterion = criterion.cuda()
    start_time = time.time()
    def trainEpoch(epoch):
        #shuffle mini batch order
        batchOrder = torch.randperm(len(trainData))
        total_loss, report_loss = 0, 0
        total_words, report_words = 0, 0
        report_src_words = 0
        start = time.time()
        N = len(trainData)
        for i in range(N):
            batchIdx = batchOrder[i] if epoch >= opt.curriculum else i
            batch = trainData[batchIdx]
            step = (i + (epoch-1) * len(trainData)) * opt.batch_size
            batch = [x.transpose(0, 1) for x in batch] # must be batch first for gather/scatter in DataParallel
            outputs = model(batch)
            model.zero_grad()
            targets = batch[1][:, 1:]  # exclude <s> from targets
            loss, loss_report = criterion.forward(outputs, targets, step=step)
            loss.backward()
            # update the parameters
            grad_norm = optim.step()
            report_loss += loss_report
            total_loss += loss_report
            report_src_words += batch[0].data.ne(onmt.Constants.PAD).sum()
            num_words = targets.data.ne(onmt.Constants.PAD).sum()
            total_words += num_words
            report_words += num_words
            if i % opt.log_interval == 0 and i > 0:
                print("Epoch %2d, %5d/%5d batches;  perplexity: %6.2f; %3.0f Source tokens/s; %6.0f s elapsed" %
                      (epoch, i, len(trainData),
                      math.exp(min(100, report_loss / report_words)),
                      report_src_words/(time.time()-start),
                      time.time()-start_time))

                report_loss = report_words = report_src_words = 0
                start = time.time()
            ### Logging
        return total_loss / total_words

    for epoch in range(opt.start_epoch, opt.epochs + 1):
        print('')
        #  (1) train for one epoch on the training set
        train_loss = trainEpoch(epoch)
        print('Train perplexity: %g' % math.exp(min(train_loss, 100)))

        ##  (2) evaluate on the validation set
        valid_loss = eval(model, criterion, validData, epoch)
        valid_ppl = math.exp(min(valid_loss, 100))
        print('Validation perplexity: %g' % valid_ppl)
        valid_loss = 100.
        valid_ppl = 1000.

        #  (3) maybe update the learning rate
        if opt.optim == 'sgd':
            optim.updateLearningRate(valid_loss, epoch)

        #  (4) drop a checkpoint
        checkpoint = {
            'model': model,
            'dicts': dataset['dicts'],
            'opt': opt,
            'epoch': epoch,
            'optim': optim,
        }
        if not epoch % 10:
            torch.save(checkpoint,
                       '%s_e%d_%.2f.pt' % (opt.save_model, epoch, valid_ppl))


def main():

    print("Loading data from '%s'" % opt.data)

    dataset = torch.load(opt.data)

    trainData = onmt.Dataset(dataset['train']['src'],
                             dataset['train']['tgt'], opt.batch_size, opt.cuda)
    validData = onmt.Dataset(dataset['valid']['src'],
                             dataset['valid']['tgt'], opt.batch_size, opt.cuda)

    dicts = dataset['dicts']
    print(' * vocabulary size. source = %d; target = %d' %
          (dicts['src'].size(), dicts['tgt'].size()))
    print(' * number of training sentences. %d' %
          len(dataset['train']['src']))
    print(' * maximum batch size. %d' % opt.batch_size)

    print('Building model...')

    if opt.train_from is None:
        encoder = onmt.Models.Encoder(opt, dicts['src'])
        decoder = onmt.Models.Decoder(opt, dicts['tgt'])
        generator = nn.Sequential(
            nn.Linear(opt.rnn_size, dicts['tgt'].size()),
            nn.LogSoftmax())
        if opt.cuda > 1:
            generator = nn.DataParallel(generator, device_ids=opt.gpus)
        model = onmt.Models.NMTModel(encoder,
                                     decoder,
                                     generator,
                                     opt)
        if opt.cuda > 1:
            model = nn.DataParallel(model, device_ids=opt.gpus)
        if opt.cuda:
            model.cuda()
        else:
            model.cpu()

        #model.generator = generator

        for p in model.parameters():
            p.data.uniform_(-opt.param_init, opt.param_init)

        optim = onmt.Optim(
            model.parameters(), opt.optim, opt.learning_rate, opt.max_grad_norm,
            lr_decay=opt.learning_rate_decay,
            start_decay_at=opt.start_decay_at
        )
    else:
        print('Loading from checkpoint at %s' % opt.train_from)
        checkpoint = torch.load(opt.train_from)
        model = checkpoint['model']
        if opt.cuda:
            model.cuda()
        else:
            model.cpu()
        optim = checkpoint['optim']
        opt.start_epoch = checkpoint['epoch'] + 1

    nParams = sum([p.nelement() for p in model.parameters()])
    print('* number of parameters: %d' % nParams)

    trainModel(model, trainData, validData, dataset, optim)


if __name__ == "__main__":
    main()
