import torch
import torch.nn as nn
from torch.autograd import Variable
import onmt.modules
import math
import numpy
from tensorboard_logger import log_value



class Encoder(nn.Module):

    def __init__(self, opt, dicts):
        self.layers = opt.layers
        self.num_directions = 2 if opt.brnn else 1
        assert opt.rnn_size % self.num_directions == 0
        self.hidden_size = opt.rnn_size // self.num_directions
        inputSize = opt.word_vec_size

        super(Encoder, self).__init__()
        self.word_lut = nn.Embedding(dicts.size(),
                                  opt.word_vec_size,
                                  padding_idx=onmt.Constants.PAD)
        self.rnn = nn.LSTM(inputSize, self.hidden_size,
                        num_layers=opt.layers,
                        dropout=opt.dropout,
                        bidirectional=opt.brnn)

        if opt.pre_word_vecs_enc is not None:
            pretrained = torch.load(opt.pre_word_vecs_enc)
            self.word_lut.weight.copy_(pretrained)

    def forward(self, input, hidden=None):
        batch_size = input.size(0) # batch first for multi-gpu compatibility
        emb = self.word_lut(input).transpose(0, 1)
        if hidden is None:
            h_size = (self.layers * self.num_directions, batch_size, self.hidden_size)
            h_0 = Variable(emb.data.new(*h_size).zero_(), requires_grad=False)
            c_0 = Variable(emb.data.new(*h_size).zero_(), requires_grad=False)
            hidden = (h_0, c_0)

        outputs, hidden_t = self.rnn(emb, hidden)
        return hidden_t, outputs



class StackedLSTM(nn.Module):
    def __init__(self, num_layers, input_size, rnn_size, dropout):
        super(StackedLSTM, self).__init__()
        self.dropout = nn.Dropout(dropout)
        self.num_layers = num_layers

        for i in range(num_layers):
            layer = nn.LSTMCell(input_size, rnn_size)
            self.add_module('layer_%d' % i, layer)
            input_size = rnn_size

    def forward(self, input, hidden):
        h_0, c_0 = hidden
        h_1, c_1 = [], []
        for i in range(self.num_layers):
            layer = getattr(self, 'layer_%d' % i)
            print 'h, c sizes:', h_0[i].size(), c_0[i].size()
            h_1_i, c_1_i = layer(input, (h_0[i], c_0[i]))
            input = h_1_i
            if i + 1 != self.num_layers:
                input = self.dropout(input)
            h_1 += [h_1_i]
            c_1 += [c_1_i]

        h_1 = torch.stack(h_1)
        c_1 = torch.stack(c_1)

        return input, (h_1, c_1)


class Decoder(nn.Module):

    def __init__(self, opt, dicts):
        self.layers = opt.layers
        self.input_feed = opt.input_feed
        input_size = opt.word_vec_size
        if self.input_feed:
            input_size += opt.rnn_size

        super(Decoder, self).__init__()
        self.word_lut = nn.Embedding(dicts.size(),
                                  opt.word_vec_size,
                                  padding_idx=onmt.Constants.PAD)
        self.rnn = StackedLSTM(opt.layers, input_size, opt.rnn_size, opt.dropout)
        self.attn = onmt.modules.GlobalAttention(opt.rnn_size)
        self.dropout = nn.Dropout(opt.dropout)
        self.hidden_size = opt.rnn_size
        self.out_size = opt.rnn_size

        if opt.pre_word_vecs_enc is not None:
            pretrained = torch.load(opt.pre_word_vecs_dec)
            self.word_lut.weight.copy_(pretrained)


    def forward(self, input, hidden, context, init_output):
        emb = self.word_lut(input).transpose(0, 1)

        batch_size = input.size(0)

        h_size = (batch_size, self.hidden_size)
        output = Variable(emb.data.new(*h_size).zero_(), requires_grad=False)

        # n.b. you can increase performance if you compute W_ih * x for all
        # iterations in parallel, but that's only possible if
        # self.input_feed=False
        outputs = []
        output = init_output
        for i, emb_t in enumerate(emb.chunk(emb.size(0), dim=0)):
            emb_t = emb_t.squeeze(0)
            if self.input_feed:
                emb_t = torch.cat([emb_t, output], 1)

            output, hidden = self.rnn(emb_t, hidden)
            output, attn = self.attn(output, context.t())
            output = self.dropout(output)
            outputs += [output]

        outputs = torch.stack(outputs)
        return outputs.transpose(0, 1), hidden, attn

class FeedForward(nn.Module):
    ''' FeedForward Module with one hidden layer
    '''
    def __init__(self, in_dim, out_dim):
        super(FeedForward, self).__init__()
        h_dim = (in_dim+out_dim) // 2
        self.linear_in = nn.Linear(in_dim, h_dim)
        self.activation = nn.Tanh()
        self.linear_out = nn.Linear(h_dim, out_dim)

    def forward(self, input):
        """
        input: in_dim
        returns: out_dim
        """
        hidden = self.linear_in(input)
        hidden = self.activation(hidden)
        out = self.linear_out(hidden)
        out = self.activation(out)
        return out

class NMTModel(nn.Module):

    def __init__(self,
                 encoder,
                 decoder,
                 generator,
                 opt):

        super(NMTModel, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.generator = generator
        self.generate = False

    def set_generate(self, enabled):
        self.generate = enabled

    def make_init_decoder_output(self, context, dec):
        batch_size = context.size(1)
        h_size = (batch_size, dec.out_size)
        return Variable(context.data.new(*h_size).zero_(), requires_grad=False)

    def _fix_enc_hidden(self, h):
        #  the encoder hidden is  (layers*directions) x batch x dim
        #  we need to convert it to layers x batch x (directions*dim)
        if self.encoder.num_directions == 2:
            return h.view(h.size(0) // 2, 2, h.size(1), h.size(2)) \
                    .transpose(1, 2).contiguous() \
                    .view(h.size(0) // 2, h.size(1), h.size(2) * 2)
        else:
            return h

    def forward(self, input):
        src = input[0]
        tgt = input[1][:, :-1]  # exclude last target from inputs
        ### Source Encoding
        enc_hidden, context = self.encoder(src)
        enc_hidden = (self._fix_enc_hidden(enc_hidden[0]),
                      self._fix_enc_hidden(enc_hidden[1]))
        ### Target Decoding
        init_output = self.make_init_decoder_output(context,
                                                    self.decoder)
        enc_hidden = (self._fix_enc_hidden(enc_hidden[0]),
                      self._fix_enc_hidden(enc_hidden[1]))
        out, dec_hidden, _attn = self.decoder(tgt,
                                              enc_hidden,
                                              context,
                                              init_output)

        if self.generate:
            out = self.generator(out)

        return out

class Loss(nn.Module):
    '''Computes Variational Loss.
    '''

    def __init__(self, opt, generator, vocabSize):
        super(Loss, self).__init__()
        self.generator = generator

    def p_theta_y(self, output, targets):
        '''Computes Log Likelihood of Targets Given X.
        '''
        output = output.contiguous().view(-1, output.size(2))
        pred = self.generator(output)
        pred = pred.view(targets.size(0), targets.size(1), pred.size(1))
        gathered = torch.gather(pred, 2,  targets.unsqueeze(2)).squeeze()
        gathered = gathered.masked_fill_(targets.eq(onmt.Constants.PAD), 0)
        pty = torch.sum(gathered.squeeze(), 1)
        return pty



    def forward(self, outputs, targets, step=None):
        loss = -self.p_theta_y(outputs, targets)
        loss_report = loss.sum().data[0]
        loss = loss.mean()
        log_value('loss', loss.data[0], step)
        log_value('loss_report', loss_report, step)
        return loss, loss_report
