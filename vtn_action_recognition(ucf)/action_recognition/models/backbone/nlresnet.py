import collections
import math

import torch
import torch.nn as nn
import torch.utils.model_zoo as model_zoo

from ...utils import drop_last


__all__ = ['nlresnet34']

model_urls = {
    'resnet18': 'https://download.pytorch.org/models/resnet18-5c106cde.pth',
    'resnet34': 'https://download.pytorch.org/models/resnet34-333f7ec4.pth',
    'resnet50': 'https://download.pytorch.org/models/resnet50-19c8e357.pth',
    'resnet101': 'https://download.pytorch.org/models/resnet101-5d3b4d8f.pth',
    'resnet152': 'https://download.pytorch.org/models/resnet152-b121ed2d.pth',
}

#########################################Spatial Temporal SNL Block#######################################################################
#######################################################################################
class STnonlocalGNLStage(nn.Module):
    def __init__(self, inplanes, planes, seq_len, stage_num=1, use_scale=False, out_num=2, relu=False, aff_kernel='dot'):
        super(STnonlocalGNLStage, self).__init__()
        self.seq_len = seq_len
        self.down_channel = planes
        self.output_channel = inplanes
        self.num_block = stage_num
        self.input_channel = inplanes
        self.softmax = nn.Softmax(dim=2)
        self.aff_kernel = aff_kernel
        self.t = nn.Conv3d(inplanes, planes, kernel_size=1, stride=1)
        self.p = nn.Conv3d(inplanes, planes, kernel_size=1, stride=1)
        self.use_scale = use_scale


        layers = []
        for i in range(stage_num):
            layers.append(STstageGNLUnit(inplanes, planes, out_num=out_num, relu=relu))

        self.stages = nn.Sequential(*layers)



        #self.stage1 = stageUnit(self.input_channel, output_channel)

        #self.stage2 = stageUnit(self.input_channel, output_channel)

        self._init_params()

        nn.init.xavier_normal_(self.t.weight, gain=0.02)
        nn.init.xavier_normal_(self.p.weight, gain=0.02)

    def _init_params(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Conv3d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm3d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def DotKernel(self, x):

        t = self.t(x)
        p = self.p(x)

        b, c, n, h, w = t.size()

        t = t.view(b, c, -1).permute(0, 2, 1)
        p = p.view(b, c, -1)

        att = torch.bmm(t, p)

        att = self.softmax(att)

        return att

    def GassKernel(self, x, gama=1e-1):

        t = self.t(x)

        b, c, h, w = t.size()

        p = t.view(b, c, -1)
        t = t.view(b, c, -1).permute(0, 2, 1)

        att = torch.bmm(t, p)

        att = torch.exp(gama * att)
                
        att = self.softmax(att)

        return att

    def RBFGassKeneral(self, x, gama=1e-4):

        print("!!!!!!!")

        t = self.t(x)
        p = self.p(x)

        b, c, h, w = t.size()

        t = t.view(b, c, -1, 1)
        p = p.view(b, c, 1, -1)

        att = t.expand(b, c, h*w, h*w) - p.expand(b, c, h*w, h*w)

        att = gama * torch.norm(att, 2, 1)

        att = att.view(b, h*w, h*w)

        att = self.softmax(att)

        return att



    def EbdedGassKernel(self, x, gama=1e-1):

        t = self.t(x)
        p = self.p(x)

        b, c, h, w = t.size()

        t = t.view(b, c, -1).permute(0, 2, 1)
        p = p.view(b, c, -1)

        att = torch.bmm(t, p)

        att = torch.exp(gama * att)

        att = self.softmax(att)

        return att

    def forward(self, x):

        x = x.view(-1, self.seq_len, x.size(1), x.size(2), x.size(3)).permute(0, 2, 1, 3, 4).contiguous()
        b, c, n, h , w = x.size()
        if self.aff_kernel == 'dot':
            att = self.DotKernel(x)
        elif self.aff_kernel == 'embedgassian':
            att = self.EbdedGassKernel(x)
        elif self.aff_kernel == "gassian":
            att = self.GassKernel(x)
        elif self.aff_kernel == 'rbf':
            att = self.RBFGassKeneral(x)
        else:
            raise KeyError("Unsupported nonlocal type: {}".format(nl_type))

        if self.use_scale:
            att = att.div(c**0.5)

        out = x

        for cur_stage in self.stages:
            out = cur_stage(out, att)

        out = out.permute(0, 2, 1, 3, 4).contiguous()
        out = out.view(b*n, c, h, w)
        return out



class STstageGNLUnit(nn.Module):
    """Spatial NL block for image classification.
       [https://github.com/facebookresearch/video-nonlocal-net].
    """
    def __init__(self, inplanes, planes, use_scale=False, out_num=2, relu=False, aff_kernel='dot'):
        self.use_scale = use_scale
        self.out_num = out_num

        super(STstageGNLUnit, self).__init__()

        self.g = nn.Conv3d(inplanes, planes, kernel_size=1, stride=1)
        self.bn = nn.BatchNorm3d(inplanes)
        self.w_1 = nn.Conv3d(planes, inplanes, kernel_size=1, stride=1)
        if self.out_num >=2:
            self.w_2 = nn.Conv3d(planes, inplanes, kernel_size=1, stride=1)
        if self.out_num ==3:
            self.w_3 = nn.Conv3d(planes, inplanes, kernel_size=1, stride=1)

        self.is_relu = relu
        if self.is_relu:
            self.relu = nn.ReLU(inplace=True)

        self._init_params()

        nn.init.xavier_normal_(self.g.weight, gain=0.02)
        nn.init.xavier_normal_(self.w_1.weight, gain=0.02)
        if self.out_num >=2:
            nn.init.xavier_normal_(self.w_2.weight, gain=0.02)
        if self.out_num ==3:
            nn.init.xavier_normal_(self.w_3.weight, gain=0.02)

    def _init_params(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Conv3d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm3d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def forward(self, x, att):
        residual = x

        g = self.g(x)

        b, c, n, h, w = g.size()

        g = g.view(b, c, -1).permute(0, 2, 1)

        x_1 = g.permute(0, 2, 1).contiguous().view(b,c,n,h,w)
        x_1 = self.w_1(x_1)

        out = x_1

        if self.out_num >=2:
            x_2 = torch.bmm(att, g)
            x_2 = x_2.permute(0, 2, 1)
            x_2 = x_2.contiguous()
            x_2 = x_2.view(b, c, n, h, w)
            x_2 = self.w_2(x_2)
            out = out + x_2

        if self.out_num == 3:
            I_n = torch.Tensor(torch.eye(g.size()[1])).cuda()
            I_n = I_n.expand(att.size())
            x_3 = torch.bmm(2*att-I_n, g)
            x_3 = x_3.permute(0, 2, 1)
            x_3 = x_3.contiguous()
            x_3 = x_3.view(b, c, n, h, w)
            x_3 = self.w_3(x_3)
            out = out + x_3

        out = self.bn(out)# + residual

        if self.is_relu:
            out = self.relu(out)

        out = out + residual

        return out
###############################################################################################################################


#########################################Spatial SNL Block#######################################################################
class SNLStage(nn.Module):
    def __init__(self, inplanes, planes, stage_num=5, use_scale=False, relu=False, aff_kernel='dot'):
        super(SNLStage, self).__init__()
        self.down_channel = planes
        self.output_channel = inplanes
        self.num_block = stage_num
        self.input_channel = inplanes
        self.softmax = nn.Softmax(dim=2)
        self.aff_kernel = aff_kernel
        self.t = nn.Conv2d(inplanes, planes, kernel_size=1, stride=1)
        self.p = nn.Conv2d(inplanes, planes, kernel_size=1, stride=1)
        self.use_scale = use_scale


        layers = []
        for i in range(stage_num):
            layers.append(SNLUnit(inplanes, planes, relu=relu))

        self.stages = nn.Sequential(*layers)
        self._init_params()


    def _init_params(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Conv3d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm3d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def DotKernel(self, x):

        t = self.t(x)
        p = self.p(x)

        b, c, h, w = t.size()

        t = t.view(b, c, -1).permute(0, 2, 1)
        p = p.view(b, c, -1)

        att = torch.bmm(t, p)

        att = self.softmax(att)

        return att


    def forward(self, x):

        if self.aff_kernel == 'dot':
            att = self.DotKernel(x)
        #elif self.aff_kernel == 'embedgassian':
        #    att = self.EbdedGassKernel(x)
        #elif self.aff_kernel == "gassian":
        #    att = self.GassKernel(x)
        #elif self.aff_kernel == 'rbf':
        #    att = self.RBFGassKeneral(x)
        else:
            raise KeyError("Unsupported nonlocal type: {}".format(nl_type))

        if self.use_scale:
            att = att.div(c**0.5)

        out = x

        for cur_stage in self.stages:
            out = cur_stage(out, att)

        return out



class SNLUnit(nn.Module):
    def __init__(self, inplanes, planes, use_scale=False, relu=False, aff_kernel='dot'):
        self.use_scale = use_scale

        super(SNLUnit, self).__init__()

        self.g = nn.Conv2d(inplanes, planes, kernel_size=1, stride=1)
        self.bn = nn.BatchNorm2d(inplanes)
        self.w_1 = nn.Conv2d(planes, inplanes, kernel_size=1, stride=1)
        self.w_2 = nn.Conv2d(planes, inplanes, kernel_size=1, stride=1)

        self.is_relu = relu
        if self.is_relu:
            self.relu = nn.ReLU(inplace=True)

        self._init_params()

    def _init_params(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Conv3d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm3d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def forward(self, x, att):
        residual = x

        g = self.g(x)

        b, c, h, w = g.size()

        g = g.view(b, c, -1).permute(0, 2, 1)

        x_1 = g.permute(0, 2, 1).contiguous().view(b,c,h,w)
        x_1 = self.w_1(x_1)

        out = x_1

        x_2 = torch.bmm(att, g)
        x_2 = x_2.permute(0, 2, 1)
        x_2 = x_2.contiguous()
        x_2 = x_2.view(b, c, h, w)
        x_2 = self.w_2(x_2)
        out = out + x_2

        out = self.bn(out)

        if self.is_relu:
            out = self.relu(out)

        out = out + residual

        return out
#################################################################################################################




def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = nn.BatchNorm2d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = nn.BatchNorm2d(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class ResNet(nn.Module):

    def __init__(self, block, layers, num_classes=1000, num_channels=3):
        self.inplanes = 64
        super(ResNet, self).__init__()
        self.conv1 = nn.Conv2d(num_channels, 64, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer_nl(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)
        self.avgpool = nn.AvgPool2d(7, stride=1)
        self.fc = nn.Linear(512 * block.expansion, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def _addNonlocal(self, in_planes, sub_planes, nl_type='nl', stage_num=None):

        if nl_type == 'snl':
            #return SNLStage(
            #    in_planes, sub_planes,
            #    use_scale=False, stage_num=1)
            return STnonlocalGNLStage(in_planes, sub_planes, 16, stage_num=1, use_scale=False, out_num=2, relu=False, aff_kernel='dot')
        else:
            raise KeyError("Unsupported nonlocal type: {}".format(nl_type))

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def _make_layer_nl(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        sub_planes = int(self.inplanes / 2)
        for i in range(1, blocks):
            if i == 5:
                layers.append(self._addNonlocal(self.inplanes, sub_planes, 'snl', 1))
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)

        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)

        return x

def load_partial_weight(model, pretrained):
    """Loads the partial weights for NL/CGNL network.
    """
    _pretrained = pretrained
    _pretrained_dict = OrderedDict()
    for k, v in _pretrained.items():
        ks = k.split('.')
        layer_name = '.'.join(ks[0:2])
        if layer_name == 'layer3.{}'.format(5):
            ks[1] = str(int(ks[1]) + 1)
            k = '.'.join(ks)
        _pretrained_dict[k] = v
    return _pretrained_dict

def chek_conv1_params(model, pretrained_weights):
    if model.conv1.in_channels != pretrained_weights['conv1.weight'].size(1):
        # get mean over RGB channels weights
        rgb_mean = torch.mean(pretrained_weights['conv1.weight'], dim=1)

        expand_ratio = model.conv1.in_channels // pretrained_weights['conv1.weight'].size(1)
        pretrained_weights['conv1.weight'] = pretrained_weights['conv1.weight'].repeat(1, expand_ratio, 1, 1)
        # pretrained_weights['conv1.weight'] = rgb_mean.unsqueeze(1).repeat(1, model.conv1.in_channels, 1, 1)


def average_conv1_weights(old_params, in_channels):
    new_params = collections.OrderedDict()
    layer_count = 0
    all_key_list = old_params.keys()
    for layer_key in drop_last(all_key_list, 2):
        if layer_count == 0:
            rgb_weight = old_params[layer_key]
            rgb_weight_mean = torch.mean(rgb_weight, dim=1)
            flow_weight = rgb_weight_mean.unsqueeze(1).repeat(1, in_channels, 1, 1)
            if isinstance(flow_weight, torch.autograd.Variable):
                new_params[layer_key] = flow_weight.data
            else:
                new_params[layer_key] = flow_weight
            layer_count += 1
        else:
            new_params[layer_key] = old_params[layer_key]
            layer_count += 1

    return new_params


def load_pretrained_resnet(model, resnet_name='resnet34', num_channels=3):
    if num_channels == 3:
        pretrained_weights = model_zoo.load_url(model_urls[resnet_name])
        chek_conv1_params(model, pretrained_weights)
        model.load_state_dict(pretrained_weights)
    else:
        pretrained_dict = model_zoo.load_url(model_urls[resnet_name])
        model_dict = model.state_dict()

        pretrained_dict = load_partial_weight(model, pretrained_dict)

        new_pretrained_dict = average_conv1_weights(pretrained_dict, num_channels)

        # 1. filter out unnecessary keys
        #new_pretrained_dict = {k: v for k, v in new_pretrained_dict.items() if k in model_dict}
        # 2. overwrite entries in the existing state dict
        model_dict.update(new_pretrained_dict)
        # 3. load the new state dict
        model.load_state_dict(model_dict)
    return model



def nlresnet34(pretrained=False, **kwargs):
    """Constructs a ResNet-34 model.

    Args:
        pretrained (bool): If True, returns a model pre-trained on ImageNet
    """
    model = ResNet(BasicBlock, [3, 4, 6, 3], **kwargs)
    num_channels = 3
    if 'num_channels' in kwargs:
        num_channels = kwargs['num_channels']
    if pretrained:
        model = load_pretrained_resnet(model, 'resnet34', num_channels)
    return model

