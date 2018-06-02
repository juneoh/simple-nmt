import numpy as np
import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack

import data_loader
from simple_nmt.search import SingleBeamSearchSpace

class Attention(nn.Module):

    def __init__(self, hidden_size):
        super(Attention, self).__init__()

        self.linear = nn.Linear(hidden_size, hidden_size, bias = False)
        self.softmax = nn.Softmax(dim = -1)

    def forward(self, h_src, h_t_tgt, mask = None):
        # |h_src| = (batch_size, length, hidden_size)
        # |h_t_tgt| = (batch_size, 1, hidden_size)
        # |mask| = (batch_size, length)

        query = self.linear(h_t_tgt.squeeze(1)).unsqueeze(-1)
        # |query| = (batch_size, hidden_size, 1)

        weight = torch.bmm(h_src, query).squeeze(-1)
        # |weight| = (batch_size, length)
        if mask is not None:
            weight.masked_fill_(mask, -float('inf'))
        weight = self.softmax(weight)

        context_vector = torch.bmm(weight.unsqueeze(1), h_src)
        # |context_vector| = (batch_size, 1, hidden_size)

        return context_vector

class Encoder(nn.Module):

    def __init__(self, word_vec_dim, hidden_size, n_layers = 4, dropout_p = .2):
        super(Encoder, self).__init__()

        self.rnn = nn.LSTM(word_vec_dim, int(hidden_size / 2), num_layers = n_layers, dropout = dropout_p, bidirectional = True, batch_first = True)

    def forward(self, emb):
        # |emb| = (batch_size, length, word_vec_dim)

        if isinstance(emb, tuple):
            x, lengths = emb
            x = pack(x, lengths.tolist(), batch_first = True)
        else:
            x = emb
        
        y, h = self.rnn(x)
        # |y| = (batch_size, length, hidden_size)
        # |h[0]| = (num_layers * 2, batch_size, hidden_size / 2)

        if isinstance(emb, tuple):
            y, _ = unpack(y, batch_first = True)

        return y, h

class Decoder(nn.Module):

    def __init__(self, word_vec_dim, hidden_size, n_layers = 4, dropout_p = .2):
        super(Decoder, self).__init__()

        self.rnn = nn.LSTM(word_vec_dim + hidden_size, hidden_size, num_layers = n_layers, dropout = dropout_p, bidirectional = False, batch_first = True)

    def forward(self, emb_t, h_t_1_tilde, h_t_1):
        # |emb_t| = (batch_size, 1, word_vec_dim)
        # |h_t_1_tilde| = (batch_size, 1, hidden_size)
        # |h_t_1[0]| = (n_layers, batch_size, hidden_size)
        batch_size = emb_t.size(0)
        hidden_size = h_t_1[0].size(-1)

        if h_t_1_tilde is None:
            h_t_1_tilde = emb_t.new(batch_size, 1, hidden_size).zero_()

        x = torch.cat([emb_t, h_t_1_tilde], dim = -1)
        y, h = self.rnn(x, h_t_1)

        return y, h

class Generator(nn.Module):
    
    def __init__(self, hidden_size, output_size):
        super(Generator, self).__init__()

        self.output = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim = -1)

    def forward(self, x):
        # |x| = (batch_size, length, hidden_size)

        y = self.softmax(self.output(x))
        # |y| = (batch_size, length, output_size)

        return y

class Seq2Seq(nn.Module):
    
    def __init__(self, input_size, word_vec_dim, hidden_size, output_size, n_layers = 4, dropout_p = .2):
        self.input_size = input_size
        self.word_vec_dim = word_vec_dim
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_layers = n_layers
        self.dropout_p = dropout_p

        super(Seq2Seq, self).__init__()

        self.emb_src = nn.Embedding(input_size, word_vec_dim)
        self.emb_dec = nn.Embedding(output_size, word_vec_dim)
        
        self.encoder = Encoder(word_vec_dim, hidden_size, n_layers = n_layers, dropout_p = dropout_p)
        self.decoder = Decoder(word_vec_dim, hidden_size, n_layers = n_layers, dropout_p = dropout_p)
        self.attn = Attention(hidden_size)

        self.concat = nn.Linear(hidden_size * 2, hidden_size)
        self.tanh = nn.Tanh()
        self.generator = Generator(hidden_size, output_size)

    def generate_mask(self, x, length):
        mask = []

        max_length = max(length)
        for l in length:
            if max_length - l > 0:
                mask += [torch.cat([x.new_ones(1, l).zero_(), x.new_ones(1, (max_length - l))], dim = -1)]
            else:
                mask += [x.new_ones(1, l).zero_()]

        mask = torch.cat(mask, dim = 0).byte()

        return mask

    def merge_encoder_hiddens(self, encoder_hiddens):
        new_hiddens = []
        new_cells = []

        hiddens, cells = encoder_hiddens

        for i in range(0, hiddens.size(0), 2):
            new_hiddens += [torch.cat([hiddens[i], hiddens[i + 1]], dim = -1)]
            new_cells += [torch.cat([cells[i], cells[i + 1]], dim = -1)]

        new_hiddens, new_cells = torch.stack(new_hiddens), torch.stack(new_cells)

        print(new_hiddens.size())

        return (new_hiddens, new_cells)

    def forward(self, src, tgt):
        batch_size = tgt.size(0)

        mask = None
        x_length = None
        if isinstance(src, tuple):
            x, x_length = src
            mask = self.generate_mask(x, x_length)
            # |mask| = (batch_size, length)
        else:
            x = src

        if isinstance(tgt, tuple):
            tgt = tgt[0]

        emb_src = self.emb_src(x)
        # |emb_src| = (batch_size, length, word_vec_dim)

        h_src, h_0_tgt = self.encoder((emb_src, x_length))
        # |h_src| = (batch_size, length, hidden_size)
        # |h_0_tgt| = (n_layers * 2, batch_size, hidden_size / 2)

        # merge bidirectional to uni-directional
        h_0_tgt, c_0_tgt = h_0_tgt
        h_0_tgt = h_0_tgt.transpose(0, 1).contiguous().view(batch_size, -1, self.hidden_size).transpose(0, 1).contiguous()
        c_0_tgt = c_0_tgt.transpose(0, 1).contiguous().view(batch_size, -1, self.hidden_size).transpose(0, 1).contiguous()
        # |h_src| = (batch_size, length, hidden_size)
        # |h_0_tgt| = (n_layers, batch_size, hidden_size)
        h_0_tgt = (h_0_tgt, c_0_tgt)

        emb_tgt = self.emb_dec(tgt)
        # |emb_tgt| = (batch_size, length, word_vec_dim)
        h_tilde = []

        h_t_tilde = None
        decoder_hidden = h_0_tgt
        for t in range(tgt.size(1)):
            emb_t = emb_tgt[:, t, :].unsqueeze(1)
            # |emb_t| = (batch_size, 1, word_vec_dim)
            # |h_t_tilde| = (batch_size, 1, hidden_size)

            decoder_output, decoder_hidden = self.decoder(emb_t, h_t_tilde, decoder_hidden)
            # |decoder_output| = (batch_size, 1, hidden_size)
            # |decoder_hidden| = (n_layers, batch_size, hidden_size)

            context_vector = self.attn(h_src, decoder_output, mask)
            # |context_vector| = (batch_size, 1, hidden_size)

            h_t_tilde = self.tanh(self.concat(torch.cat([decoder_output, context_vector], dim = -1)))
            # |h_t_tilde| = (batch_size, 1, hidden_size)

            h_tilde += [h_t_tilde]

        h_tilde = torch.cat(h_tilde, dim = 1)
        # |h_tilde| = (batch_size, length, hidden_size)

        y_hat = self.generator(h_tilde)
        # |y_hat| = (batch_size, length, output_size)

        return y_hat

    def greedy_search(self, src):
        mask = None
        x_length = None
        if isinstance(src, tuple):
            x, x_length = src
            mask = self.generate_mask(x, x_length)
        else:
            x = src
        batch_size = x.size(0)

        emb_src = self.emb_src(x)
        h_src, h_0_tgt = self.encoder((emb_src, x_length))
        h_0_tgt, c_0_tgt = h_0_tgt
        h_0_tgt = h_0_tgt.transpose(0, 1).contiguous().view(batch_size, -1, self.hidden_size).transpose(0, 1).contiguous()
        c_0_tgt = c_0_tgt.transpose(0, 1).contiguous().view(batch_size, -1, self.hidden_size).transpose(0, 1).contiguous()
        h_0_tgt = (h_0_tgt, c_0_tgt)

        y = x.new(batch_size, 1).zero_() + data_loader.BOS
        done = x.new_ones(batch_size, 1).float()
        h_t_tilde = None
        decoder_hidden = h_0_tgt
        y_hats = []
        while done.sum() > 0:
            emb_t = self.emb_dec(y)
            # |emb_t| = (batch_size, 1, word_vec_dim)

            decoder_output, decoder_hidden = self.decoder(emb_t, h_t_tilde, decoder_hidden)
            context_vector = self.attn(h_src, decoder_output, mask)
            h_t_tilde = self.tanh(self.concat(torch.cat([decoder_output, context_vector], dim = -1)))
            y_hat = self.generator(h_t_tilde)
            # |y_hat| = (batch_size, 1, output_size)
            y_hats += [y_hat]

            y = torch.topk(y_hat, 1, dim = -1)[1].squeeze(-1)
            done = done * torch.ne(y, data_loader.EOS).float()
            # |y| = (batch_size, 1)
            # |done| = (batch_size, 1)

        y_hats = torch.cat(y_hats, dim = 1)
        indice = torch.topk(y_hats, 1, dim = -1)[1].squeeze(-1)
        # |y_hat| = (batch_size, length, output_size)
        # |indice| = (batch_size, length)

        return y_hats, indice

    def batch_beam_search(self, src, beam_size = 5, max_length = 255, n_best = 1):
        mask = None
        x_length = None
        if isinstance(src, tuple):
            x, x_length = src
            mask = self.generate_mask(x, x_length)
            # |mask| = (batch_size, length)
        else:
            x = src
        batch_size = x.size(0)

        emb_src = self.emb_src(x)
        h_src, h_0_tgt = self.encoder((emb_src, x_length))
        # |h_src| = (batch_size, length, hidden_size)
        h_0_tgt, c_0_tgt = h_0_tgt
        h_0_tgt = h_0_tgt.transpose(0, 1).contiguous().view(batch_size, -1, self.hidden_size).transpose(0, 1).contiguous()
        c_0_tgt = c_0_tgt.transpose(0, 1).contiguous().view(batch_size, -1, self.hidden_size).transpose(0, 1).contiguous()
        # |h_0_tgt| = (n_layers, batch_size, hidden_size)
        h_0_tgt = (h_0_tgt, c_0_tgt)

        spaces = [SingleBeamSearchSpace((h_0_tgt[0][:, i, :].unsqueeze(1), h_0_tgt[1][:, i, :].unsqueeze(1)), None, beam_size, max_length = max_length) for i in range(batch_size)]
        done_cnt = [space.is_done() for space in spaces]

        while sum(done_cnt) < batch_size:
            current_batch_size = sum(done_cnt) * beam_size

            combined_input = []
            combined_hidden = []
            combined_cell = []
            combined_h_t_tilde = []
            combined_h_src = []
            combined_mask = []

            for i, space in enumerate(spaces):
                if space.is_done() == 0:
                    y_hat_, (hidden_, cell_), h_t_tilde_ = space.get_batch()

                    combined_input += [y_hat_]
                    combined_hidden += [hidden_]
                    combined_cell += [cell_]
                    if h_t_tilde_ is not None:
                        combined_h_t_tilde += [h_t_tilde_]
                    else:
                        combined_h_t_tilde = None

                    combined_h_src += [h_src[i, :, :]] * beam_size
                    combined_mask += [mask[i, :]] * beam_size

            combined_input = torch.cat(combined_input, dim = 0)
            combined_hidden = torch.cat(combined_hidden, dim = 1)
            combined_cell = torch.cat(combined_cell, dim = 1)
            if combined_h_t_tilde is not None:
                combined_h_t_tilde = torch.cat(combined_h_t_tilde, dim = 0)
            combined_h_src = torch.stack(combined_h_src)
            combined_mask = torch.stack(combined_mask)
            # |combined_input| = (current_batch_size, 1)
            # |combined_hidden| = (n_layers, current_batch_size, hidden_size)
            # |combined_cell| = (n_layers, current_batch_size, hidden_size)
            # |combined_h_t_tilde| = (current_batch_size, 1, hidden_size)
            # |combined_h_src| = (current_batch_size, length, hidden_size)
            # |combined_mask| = (current_batch_size, length)

            '''
            print(combined_input.size())
            print(combined_hidden.size())
            print(combined_cell.size())
            print(combined_h_t_tilde.size() if combined_h_t_tilde is not None else None)
            print(combined_h_src.size())
            print(combined_mask.size())
            '''

            emb_t = self.emb_dec(combined_input)
            # |emb_t| = (current_batch_size, 1, word_vec_dim)

            combined_decoder_output, (combined_hidden, combined_cell) = self.decoder(emb_t, combined_h_t_tilde, (combined_hidden, combined_cell))
            # |combined_decoder_output| = (current_batch_size, 1, hidden_size)
            context_vector = self.attn(combined_h_src, combined_decoder_output, combined_mask)
            # |context_vector| = (current_batch_size, 1, hidden_size)
            combined_h_t_tilde = self.tanh(self.concat(torch.cat([combined_decoder_output, context_vector], dim = -1)))
            # |combined_h_t_tilde| = (current_batch_size, 1, hidden_size)
            y_hat = self.generator(combined_h_t_tilde)
            # |y_hat| = (current_batch_size, 1, output_size)

            cnt = 0
            for space in spaces:
                if space.is_done() == 0:
                    from_index = cnt * beam_size
                    to_index = (cnt + 1) * beam_size

                    space.collect_result(y_hat[from_index:to_index],
                                                (combined_hidden[:, from_index:to_index, :], 
                                                    combined_cell[:, from_index:to_index, :]),
                                                combined_h_t_tilde[from_index:to_index]
                                                )
                    cnt += 1

            done_cnt = [space.is_done() for space in spaces]

        batch_sentences = []
        batch_probs = []

        for i, space in enumerate(spaces):
            sentences, probs = space.get_n_best(n_best)

            batch_sentences += [sentences]
            batch_probs += [probs]

        return batch_sentences, batch_probs