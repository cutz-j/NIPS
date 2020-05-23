"""
adapted from https://github.com/clovaai/stargan-v2/ (StarGAN v2)
"""

import copy
import math

from munch import Munch
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

class ResBlk(nn.Module):
    def __init__(self, dim_in, dim_out, actv=nn.LeakyReLU(0.2),
                 normalize=False, downsample=False):
        super().__init__()
        self.actv = actv
        self.normalize = normalize
        self.downsample = downsample
        self.learned_sc = dim_in != dim_out
        self._build_weights(dim_in, dim_out)

    def _build_weights(self, dim_in, dim_out):
        self.conv1 = nn.Conv2d(dim_in, dim_in, 3, 1, 1)
        self.conv2 = nn.Conv2d(dim_in, dim_out, 3, 1, 1)
        if self.normalize:
            self.norm1 = nn.InstanceNorm2d(dim_in, affine=True)
            self.norm2 = nn.InstanceNorm2d(dim_in, affine=True)
        if self.learned_sc:
            self.conv1x1 = nn.Conv2d(dim_in, dim_out, 1, 1, 0, bias=False)

    def _shortcut(self, x):
        if self.learned_sc:
            x = self.conv1x1(x)
        if self.downsample:
            x = F.avg_pool2d(x, 2)
        return x

    def _residual(self, x):
        if self.normalize:
            x = self.norm1(x)
        x = self.actv(x)
        x = self.conv1(x)
        if self.downsample:
            x = F.avg_pool2d(x, 2)
        if self.normalize:
            x = self.norm2(x)
        x = self.actv(x)
        x = self.conv2(x)
        return x

    def forward(self, x):
        x = self._shortcut(x) + self._residual(x)
        return x / math.sqrt(2)  # unit variance


class AdaIN(nn.Module):
    def __init__(self, att_dim, num_features):
        super().__init__()
        self.norm = nn.InstanceNorm2d(num_features, affine=False)
        self.fc = nn.Linear(32*32, num_features*2)
        self.occlusion_map = nn.Conv2d(3, 1, kernel_size=(9, 9), padding=(4, 4), stride=4)
        self.pool = nn.MaxPool2d(kernel_size=(2, 2), stride=2)

    def forward(self, x, s):
        h = self.pool(self.occlusion_map(s)) # (bs, 3, 256, 256) --> (bs, 1, 64, 64)
        h = torch.sigmoid(h).squeeze(1) # (bs, 32, 32)
        h = self.fc(h.view(h.size(0), h.size(1)*h.size(2))) # (bs, 32, 32) --> (bs, num_features*2)
        h = h.view(h.size(0), h.size(1), 1, 1) # (bs, num_features*2, 1, 1)
        gamma, beta = torch.chunk(h, chunks=2, dim=1) # (bs, num_features, 1, 1), (bs, num_features, 1, 1) --> split into 2: gamma, beta (dim 1)
        return (1 + gamma) * self.norm(x) + beta # (bs, c, w, h) * (bs, c, 1, 1)

class AdainResBlk(nn.Module):
    def __init__(self, dim_in, dim_out, att_dim=512, actv=nn.LeakyReLU(0.2), upsample=False):
        super().__init__()
        self.actv = actv
        self.upsample = upsample
        self.learned_sc = dim_in != dim_out
        self._build_weights(dim_in, dim_out, att_dim)

    def _build_weights(self, dim_in, dim_out, att_dim=512):
        self.conv1 = nn.Conv2d(dim_in, dim_out, 3, 1, 1)
        self.conv2 = nn.Conv2d(dim_out, dim_out, 3, 1, 1)
        self.norm1 = AdaIN(att_dim, dim_in)
        self.norm2 = AdaIN(att_dim, dim_out)
        if self.learned_sc:
            self.conv1x1 = nn.Conv2d(dim_in, dim_out, 1, 1, 0, bias=False)

    def _shortcut(self, x):
        if self.upsample:
            x = F.interpolate(x, scale_factor=2, mode='nearest')
        if self.learned_sc:
            x = self.conv1x1(x)
        return x

    def _residual(self, x, att):
        x = self.norm1(x, att)
        x = self.actv(x)
        if self.upsample:
            x = F.interpolate(x, scale_factor=2, mode='nearest')
        x = self.conv1(x)
        x = self.norm2(x, att)
        x = self.actv(x)
        x = self.conv2(x)
        return x

    def forward(self, x, att):
        out = self._residual(x, att)
        return out


class Generator(nn.Module):
    # based on resnet
    def __init__(self, img_size=256, max_conv_dim=512):
        super().__init__()
        dim_in = 2**14 // img_size # ?
        self.img_size = img_size
        self.from_rgb = nn.Conv2d(3, dim_in, 3, 1, 1)
        self.encode = nn.ModuleList()
        self.decode = nn.ModuleList()
        self.to_rgb = nn.Sequential(
            nn.InstanceNorm2d(dim_in, affine=True),
            nn.LeakyReLU(0.2),
            nn.Conv2d(dim_in, 3, 1, 1, 0))

        # down/up-sampling blocks
        repeat_num = int(np.log2(img_size)) - 4
        for _ in range(repeat_num):
            dim_out = min(dim_in*2, max_conv_dim)
            self.encode.append(ResBlk(dim_in, dim_out, normalize=True, downsample=True))
            self.decode.insert(0,AdainResBlk(dim_out, dim_in, max_conv_dim,upsample=True))  # stack-like
                
            dim_in = dim_out

        # bottleneck blocks
        for _ in range(2):
            self.encode.append(ResBlk(dim_out, dim_out, normalize=True))
            self.decode.insert(0, AdainResBlk(dim_out, dim_out))

    def forward(self, x, t, s, mask=None):
        x = self.from_rgb(x)
        t = self.from_rgb(t)
        for block in self.encode:
            x = block(x)
            t = block(t)
        for block in self.decode:
            x = block(x, s)
            # mask = F.interpolate(mask[:, [0]], size=x.size(2), mode='bilinear')
            # x = x + mask
        return self.to_rgb(x)


class Discriminator(nn.Module):
    def __init__(self, img_size=256, num_class=1, max_conv_dim=512):
        super().__init__()
        dim_in = 2**14 // img_size
        blocks = []
        blocks += [nn.Conv2d(3, dim_in, 3, 1, 1)]

        repeat_num = int(np.log2(img_size)) - 2
        for _ in range(repeat_num):
            dim_out = min(dim_in*2, max_conv_dim)
            blocks += [ResBlk(dim_in, dim_out, downsample=True)]
            dim_in = dim_out

        blocks += [nn.LeakyReLU(0.2)]
        blocks += [nn.Conv2d(dim_out, dim_out, 4, 1, 0)]
        blocks += [nn.LeakyReLU(0.2)]
        blocks += [nn.Conv2d(dim_out, num_class, 1, 1, 0)]
        self.main = nn.Sequential(*blocks)

    def forward(self, x):
        out = self.main(x)
        out = out.view(out.size(0), -1)  # (batch, num_class)
        return out

class ScaledDotProductAttention(nn.Module):
    def __init__(self, d_k=64):
        super(ScaledDotProductAttention, self).__init__()
        self.d_k = d_k
        
    def forward(self, Q, K, V):
        scores = torch.matmul(Q, K.transpose(-1, -2)) / np.sqrt(self.d_k) # scores : [batch_size x n_heads x len_q(=len_k) x len_k(=len_q)]
        attn = nn.Softmax(dim=-1)(scores)
        context = torch.matmul(attn, V)
        return context, attn

class MultiHeadAttention(nn.Module):
    def __init__(self, args):
        super(MultiHeadAttention, self).__init__()
        self.n_heads, self.d_k, self.d_v, self.dim  = args.n_heads, args.d_k, args.d_v, args.dim
        self.W_Q = nn.Linear(args.dim, args.d_k * args.n_heads)
        self.W_K = nn.Linear(args.dim, args.d_k * args.n_heads)
        self.W_V = nn.Linear(args.dim, args.d_v * args.n_heads)
        self.output = nn.Linear(args.n_heads * args.d_v, args.dim)
        self.norm = nn.LayerNorm(args.dim)
    def forward(self, Q, K, V):
        # q: [batch_size x len_q x d_model], k: [batch_size x len_k x d_model], v: [batch_size x len_k x d_model]
        residual, batch_size = Q, Q.size(0)
        # (B, S, D) -proj-> (B, S, D) -split-> (B, S, H, W) -trans-> (B, H, S, W)
        q_s = self.W_Q(Q).view(batch_size, -1, self.n_heads, self.d_k).transpose(1,2)  # q_s: [batch_size x n_heads x len_q x d_k]
        k_s = self.W_K(K).view(batch_size, -1, self.n_heads, self.d_k).transpose(1,2)  # k_s: [batch_size x n_heads x len_k x d_k]
        v_s = self.W_V(V).view(batch_size, -1, self.n_heads, self.d_v).transpose(1,2)  # v_s: [batch_size x n_heads x len_k x d_v]
        # context: [batch_size x n_heads x len_q x d_v], attn: [batch_size x n_heads x len_q(=len_k) x len_k(=len_q)]
        context, attn = ScaledDotProductAttention()(q_s, k_s, v_s)
        context = context.transpose(1, 2).contiguous().view(batch_size, -1, self.n_heads * self.d_v) # context: [batch_size x len_q x n_heads * d_v]
        output = self.output(context)
        return self.norm(output + residual), attn # output: [batch_size x len_q x d_model]

class PoswiseFeedForwardNet(nn.Module):
    def __init__(self, args):
        super(PoswiseFeedForwardNet, self).__init__()
        self.n_heads, self.d_k, self.d_v, self.dim  = args.n_heads, args.d_k, args.d_v, args.dim
        self.conv1 = nn.Conv1d(in_channels=args.dim, out_channels=args.d_ff, kernel_size=1)
        self.conv2 = nn.Conv1d(in_channels=args.d_ff, out_channels=args.dim, kernel_size=1)
        self.norm = nn.LayerNorm(args.dim)
        self.relu = nn.ReLU()

    def forward(self, inputs):
        residual = inputs # inputs : [batch_size, len_q, d_model]
        output = self.relu(self.conv1(inputs.transpose(1, 2)))
        output = self.conv2(output).transpose(1, 2)
        return self.norm(output + residual)

class EncoderLayer(nn.Module):
    def __init__(self, args):
        super(EncoderLayer, self).__init__()
        self.enc_self_attn = MultiHeadAttention(args)
        self.pos_ffn = PoswiseFeedForwardNet(args)

    def forward(self, enc_inputs):
        enc_outputs, attn = self.enc_self_attn(enc_inputs, enc_inputs, enc_inputs) # enc_inputs to same Q,K,V
        enc_outputs = self.pos_ffn(enc_outputs) # enc_outputs: [batch_size x len_q x d_model]
        return enc_outputs, attn

class DecoderLayer(nn.Module):
    def __init__(self, args):
        super(DecoderLayer, self).__init__()
        self.dec_self_attn = MultiHeadAttention(args)
        self.dec_enc_attn = MultiHeadAttention(args)
        self.pos_ffn = PoswiseFeedForwardNet(args)

    def forward(self, dec_inputs, enc_outputs):
        dec_outputs, dec_self_attn = self.dec_self_attn(dec_inputs, dec_inputs, dec_inputs)
        dec_outputs, dec_enc_attn = self.dec_enc_attn(dec_outputs, enc_outputs, enc_outputs)
        dec_outputs = self.pos_ffn(dec_outputs)
        return dec_outputs, dec_self_attn, dec_enc_attn

class Encoder(nn.Module):
    def __init__(self, args):
        super(Encoder, self).__init__()
        self.layers = nn.ModuleList([EncoderLayer(args) for _ in range(args.n_layers)])

    def forward(self, enc_inputs): # enc_inputs : (bs, 1, 512)
        enc_self_attns = []
        for layer in self.layers:
            enc_outputs, enc_self_attn = layer(enc_inputs) # Multihead --> poswisefeed
            enc_self_attns.append(enc_self_attn)
        return enc_outputs, enc_self_attns

class Decoder(nn.Module):
    def __init__(self, args):
        super(Decoder, self).__init__()
        self.layers = nn.ModuleList([DecoderLayer(args) for _ in range(args.n_layers)])

    def forward(self, dec_inputs, enc_inputs, enc_outputs): # dec_inputs : (bs, 256, 512)
        dec_self_attns, dec_enc_attns = [], []
        for layer in self.layers:
            dec_outputs, dec_self_attn, dec_enc_attn = layer(dec_inputs, enc_outputs)
            dec_self_attns.append(dec_self_attn)
            dec_enc_attns.append(dec_enc_attn)
        return dec_outputs, dec_self_attns, dec_enc_attns

class Transformer(nn.Module):
    def __init__(self, args):
        super(Transformer, self).__init__()
        self.encoder = Encoder(args)
        self.decoder = Decoder(args)
        self.stem = nn.Conv2d(args.input_dim, 1, kernel_size=(7, 7), padding=(3, 3), stride=2)
        self.projection_linear = nn.Linear(args.hidden_size, args.max_conv_dim, bias=False)
        self.reprojection_linear = nn.Linear(args.max_conv_dim, args.hidden_size, bias=False)
        self.final = nn.Conv2d(1, args.input_dim, kernel_size=(7, 7), padding=(3, 3))
     
    def forward(self, enc_inputs, dec_inputs):
        # enc_input: (bs, 3, 256, 256)
        # dec_input: (bs, 3, 256, 256)
        # Transformer
        e, d = enc_inputs, dec_inputs
        enc_inputs = self.stem(enc_inputs).squeeze(1) # (bs, 3, 256, 256) --> (bs, 128, 128)
        dec_inputs = self.stem(dec_inputs).squeeze(1) # (bs, 3, 256, 256) --> (bs, 128, 128)
        enc_inputs = self.projection_linear(enc_inputs) # (bs, 128, 128) --> (bs, 128, 512)
        dec_inputs = self.projection_linear(dec_inputs) # (bs, 128, 128) --> (bs, 128, 512)
        enc_outputs, enc_self_attns = self.encoder(enc_inputs) # (bs, 128, 512)
        dec_outputs, dec_self_attns, dec_enc_attns = self.decoder(dec_inputs, enc_inputs, enc_outputs) # (bs, 128, 512)
        dec_outputs = self.reprojection_linear(dec_outputs) # (bs, 128, 512) --> (bs, 128, 128)
        dec_outputs = F.interpolate(self.final(dec_outputs.unsqueeze(1)), scale_factor=2, mode='nearest') # (bs, 1, 128, 128) --> (bs, 3, 256, 256)
        return dec_outputs



def build_model(args):
    transformer = Transformer(args)
    generator = Generator(args.img_size, args.max_conv_dim)

    discriminator = Discriminator(args.img_size, args.num_class)
    generator_ema = copy.deepcopy(generator)

    nets = Munch(generator=generator, discriminator=discriminator, transformer=transformer)
    nets_ema = Munch(generator=generator_ema)

    return nets, nets_ema