import numpy as np
import math

import torch
import torch.nn as nn
import torch.nn.functional as F

from collections import OrderedDict

from .utils import crop_to_shape, crop_op

####
class Net(nn.Module):
    """ 
    A base class provides a common weight initialization scheme.
    """

    def weights_init(self):
        for m in self.modules():
            classname = m.__class__.__name__

            # ! Fixed the type checking
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(
                    m.weight, mode='fan_out', nonlinearity='relu')

            if 'norm' in classname.lower():
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

            if 'linear' in classname.lower():
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def forward(self, x):
        return x

####
class TFSamepaddingLayer(Net):
    '''
    To align with tf `same` padding. 
    Putting this before any conv layer that need padding
    Assuming kernel has Height == Width for simplicity
    '''

    def __init__(self, ksize, stride):
        super().__init__()
        self.ksize = ksize
        self.stride = stride

    def forward(self, x):
        if x.shape[2] % self.stride == 0:
            pad = max(self.ksize - self.stride, 0)
        else:
            pad = max(self.ksize - (x.shape[2] % self.stride), 0)

        if pad % 2 == 0:
            pad_val = pad // 2
            padding = (pad_val, pad_val, pad_val, pad_val)
        else:
            pad_val_start = pad // 2
            pad_val_end = pad - pad_val_start
            padding = (pad_val_start, pad_val_end, pad_val_start, pad_val_end)
        x = F.pad(x, padding, "constant", 0)
        return x


####
class DenseBlock(Net):
    '''
    Only perform `valid` convolution 
    '''

    def __init__(self, in_ch, unit_ksize, unit_ch, unit_count, split=1):
        super().__init__()
        assert len(unit_ksize) == len(unit_ch), 'Unbalance Unit Info'

        self.nr_unit = unit_count
        self.in_ch = in_ch
        self.unit_ch = unit_ch

        # ! For inference only so init values for batchnorm may not match tensorflow
        unit_in_ch = in_ch
        self.units = nn.ModuleList()
        for _ in range(unit_count):
            self.units.append(nn.Sequential(OrderedDict([
                ('preact_bn'  , nn.BatchNorm2d(unit_in_ch, eps=1e-5)),
                ('preact_relu', nn.ReLU(inplace=True)),

                ('conv1'     , nn.Conv2d(unit_in_ch, unit_ch[0], unit_ksize[0],
                                    stride=1, padding=0, bias=False)),
                ('conv1_bn'  , nn.BatchNorm2d(unit_ch[0], eps=1e-5)),
                ('conv1_relu', nn.ReLU(inplace=True)),

                ('conv2'     , nn.Conv2d(unit_ch[0], unit_ch[1], unit_ksize[1],
                                    groups=split, stride=1, padding=0, bias=False)),
            ])))
            unit_in_ch += unit_ch[1]

        self.blk_bna = nn.Sequential(OrderedDict([
            ('bn'  , nn.BatchNorm2d(unit_in_ch, eps=1e-5)),
            ('relu', nn.ReLU(inplace=True)),
        ]))

    def forward(self, prev_feat):
        for idx in range(self.nr_unit):
            new_feat = self.units[idx](prev_feat)
            prev_feat = crop_to_shape(prev_feat, new_feat)
            prev_feat = torch.cat([prev_feat, new_feat], dim=1)
        prev_feat = self.blk_bna(prev_feat)

        return prev_feat


####
class ResidualBlock(Net):
    def __init__(self, in_ch, unit_ksize, unit_ch, unit_count, stride=1):
        super().__init__()
        assert len(unit_ksize) == len(unit_ch), 'Unbalance Unit Info'

        self.nr_unit = unit_count
        self.in_ch = in_ch
        self.unit_ch = unit_ch

        # ! For inference only so init values for batchnorm may not match tensorflow
        unit_in_ch = in_ch
        self.units = nn.ModuleList()
        for idx in range(unit_count):
            unit_layer = [
                ('preact_bn'  , nn.BatchNorm2d(unit_in_ch, eps=1e-5)),
                ('preact_relu', nn.ReLU(inplace=True)),

                ('conv1', nn.Conv2d(unit_in_ch, unit_ch[0], unit_ksize[0],
                                    stride=1, padding=0, bias=False)),
                ('conv1_bn'  , nn.BatchNorm2d(unit_ch[0], eps=1e-5)),
                ('conv1_relu', nn.ReLU(inplace=True)),

                ('conv2_pad', TFSamepaddingLayer(ksize=unit_ksize[1],
                                                 stride=stride if idx == 0 else 1)),
                ('conv2'    , nn.Conv2d(unit_ch[0], unit_ch[1], unit_ksize[1],
                                    stride=stride if idx == 0 else 1,
                                    padding=0, bias=False)),
                ('conv2_bn'  , nn.BatchNorm2d(unit_ch[1], eps=1e-5)),
                ('conv2_relu', nn.ReLU(inplace=True)),

                ('conv3', nn.Conv2d(unit_ch[1], unit_ch[2], unit_ksize[2],
                                    stride=1, padding=0, bias=False)),
            ]
            # * has bna to conclude each previous block so
            # * must not put preact for the first unit of this block
            unit_layer = unit_layer if idx != 0 else unit_layer[2:]
            self.units.append(nn.Sequential(OrderedDict(unit_layer)))
            unit_in_ch = unit_ch[-1]

        if in_ch != unit_ch[-1] or stride != 1:
            self.shortcut = nn.Conv2d(
                in_ch, unit_ch[-1], 1, stride=stride, bias=False)
        else:
            self.shortcut = None

        self.blk_bna = nn.Sequential(OrderedDict([
            ('bn', nn.BatchNorm2d(unit_in_ch, eps=1e-5)),
            ('relu', nn.ReLU(inplace=True)),
        ]))

    def out_ch(self):
        return self.unit_ch[-1]

    def forward(self, prev_feat, freeze=False):
        if self.shortcut is None:
            shortcut = prev_feat
        else:
            shortcut = self.shortcut(prev_feat)

        # if self.training:
        #     for idx in range(0, len(self.units)):
        #         new_feat = prev_feat
        #         # * internal flag doesnt play well with external grad flag
        #         # * so trigger run according to mode is more accurate
        #         # with torch.set_grad_enabled(not freeze):
        #         new_feat = self.units[idx](new_feat)
        #         prev_feat = new_feat + shortcut
        #         shortcut = prev_feat
        # else:
        for idx in range(0, len(self.units)):
            new_feat = prev_feat
            new_feat = self.units[idx](new_feat)
            prev_feat = new_feat + shortcut
            shortcut = prev_feat
        feat = self.blk_bna(prev_feat)
        return feat


####
class UpSample2x(nn.Module):
    '''
    Assume input is of NCHW, port FixedUnpooling
    '''

    def __init__(self):
        super().__init__()
        # correct way to create constant within module
        self.register_buffer('unpool_mat', torch.from_numpy(
            np.ones((2, 2), dtype='float32')))
        self.unpool_mat.unsqueeze(0)

    def forward(self, x):
        input_shape = list(x.shape)
        # unsqueeze is expand_dims equivalent
        # permute is transpose equivalent
        # view is reshape equivalent
        x = x.unsqueeze(-1)  # bchwx1
        mat = self.unpool_mat.unsqueeze(0)  # 1xshxsw
        ret = torch.tensordot(x, mat, dims=1)  # bxcxhxwxshxsw
        ret = ret.permute(0, 1, 2, 4, 3, 5)
        ret = ret.reshape((-1, input_shape[1], input_shape[2] * 2, input_shape[3] * 2))
        return ret

####
import time
class HoVerNet(Net):
    def __init__(self, input_ch, nr_types=None, freeze=False):
        super().__init__()
        self.freeze = freeze

        self.id = time.time()

        self.conv0 = nn.Sequential(
            OrderedDict([
                ('conv', nn.Conv2d(input_ch, 64, 7, stride=1, padding=0, bias=False)),
                ('bn'  , nn.BatchNorm2d(64, eps=1e-5)),
                ('relu', nn.ReLU(inplace=True)),
            ]))

        self.d0 = ResidualBlock(64  , [1, 3, 1], [ 64,  64,  256], 3, stride=1)
        self.d1 = ResidualBlock(256 , [1, 3, 1], [128, 128,  512], 4, stride=2)
        self.d2 = ResidualBlock(512 , [1, 3, 1], [256, 256, 1024], 6, stride=2)
        self.d3 = ResidualBlock(1024, [1, 3, 1], [512, 512, 2048], 3, stride=2)

        self.conv_bot = nn.Conv2d(
            2048, 1024, 1, stride=1, padding=0, bias=False)

        # def create_decoder_branch(out_ch=2):
        #     u3 = nn.Sequential(OrderedDict([
        #         ('conva', nn.Conv2d(1024, 256, 5, stride=1, padding=0, bias=False)),
        #         ('dense', DenseBlock(256, [1, 5], [128, 32], 8, split=4)),
        #         ('convf', nn.Conv2d(512, 512, 1, stride=1, padding=0, bias=False)),
        #     ]))
        #     u2 = nn.Sequential(OrderedDict([
        #         ('conva', nn.Conv2d(512, 128, 5, stride=1, padding=0, bias=False)),
        #         ('dense', DenseBlock(128, [1, 5], [128, 32], 4, split=4)),
        #         ('convf', nn.Conv2d(256, 256, 1, stride=1, padding=0, bias=False)),
        #     ]))
        #     u1 = nn.Sequential(OrderedDict([
        #         ('conva_pad', TFSamepaddingLayer(ksize=5, stride=1)),
        #         ('conva'    , nn.Conv2d(256, 64, 5, stride=1, padding=0, bias=False)),
        #     ]))

        #     u0 = nn.Sequential(OrderedDict([
        #         ('bn', nn.BatchNorm2d(64, eps=1e-5)),
        #         ('relu', nn.ReLU(inplace=True)),
        #         ('conv', nn.Conv2d(64, out_ch, 1, stride=1, padding=0, bias=True)),
        #     ]))

        #     decoder = nn.Sequential(OrderedDict([
        #         ('u3', u3),
        #         ('u2', u2),
        #         ('u1', u1),
        #         ('u0', u0),
        #     ]))
        #     return decoder

        # if nr_types is None:
        #     self.decoder = nn.ModuleDict(
        #         OrderedDict([
        #             ('np', create_decoder_branch(out_ch=2)),
        #             ('hv', create_decoder_branch(out_ch=2)),
        #         ])
        #     )
        # else:
        #     self.decoder = nn.ModuleDict(
        #         OrderedDict([
        #             ('tp', create_decoder_branch(out_ch=nr_types)),
        #             ('np', create_decoder_branch(out_ch=2)),
        #             ('hv', create_decoder_branch(out_ch=2)),
        #         ])
        #     )

        # self.upsample2x = UpSample2x()
        # # TODO: pytorch still require the channel eventhough its ignored
        # self.weights_init()
        # self.check_output_shape([3, 270, 270])

    def forward(self, imgs, print_size=False):

        imgs = imgs / 255.0  # to 0-1 range to match XY

        # if self.training:
        #     d0 = self.conv0(imgs)
        #     d0 = self.d0(d0, self.freeze)
        #     # * internal flag doesnt play well with external grad flag
        #     # * so trigger run according to mode is more accurate
        #     with torch.set_grad_enabled(not self.freeze):
        #         d1 = self.d1(d0)
        #         d2 = self.d2(d1)
        #         d3 = self.d3(d2)
        #     d3 = self.conv_bot(d3)
        # else:
        d0 = self.conv0(imgs)
        d0 = self.d0(d0)
        d1 = self.d1(d0)
        d2 = self.d2(d1)
        d3 = self.d3(d2)
        d3 = self.conv_bot(d3)
        # d = [d0, d1, d2, d3]

        # TODO: switch to `crop_to_shape` ?
        # d[0] = crop_op(d[0], [184, 184])
        # d[1] = crop_op(d[1], [72, 72])

        # out_dict = {}
        # for branch_name, branch_desc in self.decoder.items():
        #     u3 = self.upsample2x(d[-1]) + d[-2]
        #     u3 = branch_desc[0](u3)

        #     u2 = self.upsample2x(u3) + d[-3]
        #     u2 = branch_desc[1](u2)

        #     u1 = self.upsample2x(u2) + d[-4]
        #     u1 = branch_desc[2](u1)

        #     u0 = branch_desc[3](u1)
        #     out_dict[branch_name] = u0

        return d3