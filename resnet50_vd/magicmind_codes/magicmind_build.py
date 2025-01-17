import os
import json
import argparse
import numpy as np
import magicmind.python.runtime as mm
from magicmind.python.runtime.parser import Parser
from magicmind.python.common.types import get_datatype_by_numpy
from typing import List
from torchvision import transforms
from PIL import Image
import logging

def imagenet_dataset(
    val_txt="/nfsdata/datasets/imageNet2012/labels.txt",
    image_file_path="/nfsdata/datasets/imageNet2012/",
    count=-1
):
    with open(val_txt, "r") as f:
        lines = f.readlines()
    logging.info("%d pictures will be read." % len(lines))
    current_count = 0
    for line in lines:
        image_name, label = line.split(" ")
        image_path = os.path.join(image_file_path, image_name)
        img = Image.open(image_path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        yield img, label.strip()
        current_count += 1
        if current_count >= count and count != -1:
            break
def preprocess(input_image, transpose):
    resize_h, resize_w = (256, 256)
    crop_h, crop_w = (224, 224)
    mean = [0.485, 0.456, 0.406]
    std = [0.229, 0.224, 0.225]
    normalize = transforms.Normalize(mean, std)
    preprocess = transforms.Compose(
        [
            transforms.Resize(resize_h),
            transforms.CenterCrop(crop_h),
            transforms.ToTensor(),
            normalize,
        ]
    )
    input_tensor = preprocess(input_image)
    input_numpy = input_tensor.numpy()
    if transpose:
        input_numpy = np.transpose(input_numpy, (1, 2, 0))
    return input_numpy

class CalibData(mm.CalibDataInterface):
    def __init__(self, data_list: List[np.ndarray]):
        super(CalibData, self).__init__()
        self._data_list = data_list
        self._cur_idx = 0
        status = self.reset()
        assert status.ok(), str(status)

    def next(self):
        if self._cur_idx >= len(self._data_list):
            return mm.Status(mm.Code.OUT_OF_RANGE, "No more data.")
        self._data = np.ascontiguousarray(self._data_list[self._cur_idx])
        self._data_shape = mm.Dims(self._data.shape)
        self._data_type = get_datatype_by_numpy(self._data.dtype)
        self._cur_idx += 1
        return mm.Status()

    def get_shape(self):
        return self._data_shape

    def get_data_type(self):
        return self._data_type

    def get_sample(self):
        return self._data

    def reset(self):
        self._cur_idx = 0
        return mm.Status()
    
def generate_model_config(args):
    config = mm.BuilderConfig()
 
    # 指定硬件平台
    assert config.parse_from_string('{"archs":[{"mtp_372": [4]}]}').ok()
    assert config.parse_from_string('{"opt_config":{"type64to32_conversion":true}}').ok()
    assert config.parse_from_string('{"opt_config":{"conv_scale_fold":true}}').ok()
    assert config.parse_from_string(
    """{"cross_compile_toolchain_path": "/tmp/gcc-linaro-6.2.1-2016.11-x86_64_aarch64-linux-gnu/"}""").ok()
    # 输入数据摆放顺序
    # Caffe模型输入数据顺序为NCHW，如下代码转为NHWC输入顺序。
    # 输入顺序的改变需要同步到推理过程中的网络预处理实现，保证预处理结果的输入顺序与网络输入数据顺序一致。
    # 以下JSON字符串中的0代表改变的是网络第一个输入的数据摆放顺序。1则代表第二个输入，以此类推。
    assert config.parse_from_string('{"computation_preference": "fast"}').ok()
    assert config.parse_from_string('{"convert_input_layout": {"0": {"src": "NCHW", "dst": "NHWC"}}}').ok()
    # 模型输入输出规模可变功能
    if args.shape_mutable == "true":
        assert config.parse_from_string('{"graph_shape_mutable":true}').ok()
        assert config.parse_from_string('{"dim_range": {"0": {"min": [1, 3, 224, 224], "max": [512, 3, 224, 224]}}}').ok()
    else:
        assert config.parse_from_string('{"graph_shape_mutable":false}').ok()
    # 精度模式
    assert config.parse_from_string('{"precision_config":{"precision_mode":"%s"}}' % args.quant_mode).ok()
    # 量化算法，支持对称量化（symmetric)和非对称量化（asymmetric）。当量化统计算法设置为EQNM_ALOGORITHM时，仅适用于对称量化。
    assert config.parse_from_string('{"precision_config": {"activation_quant_algo": "symmetric"}}').ok()
    # 设置量化粒度，支持按tensor量化（per_tensor）和按通道量化（per_axis）两种。
    assert config.parse_from_string('{"precision_config": {"weight_quant_granularity": "per_tensor"}}').ok()
    return config
 
def calibrate(args, network : mm.Network, config : mm.BuilderConfig):
    # 创建量化工具并设置量化统计算法
    dataset = imagenet_dataset(val_txt =args.label_file, image_file_path = args.image_dir, count=10)
    sample_data = []
    for data in dataset:
        data = preprocess(data, transpose=False)
        data = np.expand_dims(data, 0)
        if args.batch_size > 1:
            data = data.repeat(args.batch_size, axis=0)
            print(data.shape)
        sample_data.append(data)
 
    calib_data = CalibData(sample_data)
    calibrator = mm.Calibrator([calib_data])
    assert calibrator is not None
    # 设置量化统计算法，支持线性统计算法（LINEAR_ALGORITHM）及加强的最小化量化噪声算法（EQM_ALGORITHM）。
    assert calibrator.set_quantization_algorithm(mm.QuantizationAlgorithm.LINEAR_ALGORITHM).ok()
 
    # 打开设备
    with mm.System() as mm_sys:
        dev_count = mm_sys.device_count()
        print("Device count: ", dev_count)
        if args.device_id >= dev_count:
            print("Invalid device set!")
            # 打开MLU设备
            dev = mm.Device()
            dev.id = args.device_id
            assert dev.active().ok()
            print("Wroking on MLU ", args.device_id)
    # 进行量化
    assert calibrator.calibrate(network, config).ok()
 
def onnx_parser(args):
    # 创建MagicMind parser
    parser = Parser(mm.ModelKind.kOnnx)
    # 使用parser将Caffe模型文件转换为MagicMind Network实例。
    network = mm.Network()
    assert parser.parse(network, args.onnx_model).ok()
    # 设置网络输入数据形状
    input_dims = mm.Dims((args.batch_size, 3, args.input_height, args.input_width))
    assert network.get_input(0).set_dimension(input_dims).ok()
    return network
 
def generate_model_config(args):
    config = mm.BuilderConfig()
 
    # 指定硬件平台
    assert config.parse_from_string('{"archs":[{"mtp_372": [4]}]}').ok()
    assert config.parse_from_string('{"opt_config":{"type64to32_conversion":true}}').ok()
    assert config.parse_from_string('{"opt_config":{"conv_scale_fold":true}}').ok()
    assert config.parse_from_string(
    """{"cross_compile_toolchain_path": "/tmp/gcc-linaro-6.2.1-2016.11-x86_64_aarch64-linux-gnu/"}""").ok()
    # 输入数据摆放顺序
    # Caffe模型输入数据顺序为NCHW，如下代码转为NHWC输入顺序。
    # 输入顺序的改变需要同步到推理过程中的网络预处理实现，保证预处理结果的输入顺序与网络输入数据顺序一致。
    # 以下JSON字符串中的0代表改变的是网络第一个输入的数据摆放顺序。1则代表第二个输入，以此类推。
    assert config.parse_from_string('{"computation_preference": "fast"}').ok()
    assert config.parse_from_string('{"convert_input_layout": {"0": {"src": "NCHW", "dst": "NHWC"}}}').ok()
    # 模型输入输出规模可变功能
    if args.shape_mutable == "true":
        assert config.parse_from_string('{"graph_shape_mutable":true}').ok()
        assert config.parse_from_string('{"dim_range": {"0": {"min": [1, 3, 224, 224], "max": [512, 3, 224, 224]}}}').ok()
    else:
        assert config.parse_from_string('{"graph_shape_mutable":false}').ok()
    # 精度模式
    assert config.parse_from_string('{"precision_config":{"precision_mode":"%s"}}' % args.quant_mode).ok()
    # 量化算法，支持对称量化（symmetric)和非对称量化（asymmetric）。当量化统计算法设置为EQNM_ALOGORITHM时，仅适用于对称量化。
    assert config.parse_from_string('{"precision_config": {"activation_quant_algo": "symmetric"}}').ok()
    # 设置量化粒度，支持按tensor量化（per_tensor）和按通道量化（per_axis）两种。
    assert config.parse_from_string('{"precision_config": {"weight_quant_granularity": "per_tensor"}}').ok()
    return config
 
def calibrate(args, network : mm.Network, config : mm.BuilderConfig):
    # 创建量化工具并设置量化统计算法
    dataset = imagenet_dataset(val_txt =args.label_file, image_file_path = args.image_dir, count=10)
    sample_data = []
    for data, label in dataset:
        data = preprocess(data, transpose=False)
        data = np.expand_dims(data, 0)
        if args.batch_size > 1:
            data = data.repeat(args.batch_size, axis=0)
        sample_data.append(data)
    calib_data = CalibData(sample_data)
    calibrator = mm.Calibrator([calib_data])
    assert calibrator is not None
    # 设置量化统计算法，支持线性统计算法（LINEAR_ALGORITHM）及加强的最小化量化噪声算法（EQM_ALGORITHM）。
    assert calibrator.set_quantization_algorithm(mm.QuantizationAlgorithm.LINEAR_ALGORITHM).ok()
 
    # 打开设备
    with mm.System() as mm_sys:
        dev_count = mm_sys.device_count()
        print("Device count: ", dev_count)
        if args.device_id >= dev_count:
            print("Invalid device set!")
            # 打开MLU设备
            dev = mm.Device()
            dev.id = args.device_id
            assert dev.active().ok()
            print("Wroking on MLU ", args.device_id)
    # 进行量化
    assert calibrator.calibrate(network, config).ok()
 
def main():
    args = argparse.ArgumentParser()
    args.add_argument("--onnx_model", "--onnx_model", type=str, default="../export/resnet50-v1-7.onnx", help="original resnet50 onnx")
    args.add_argument("--output_model", "--output_model", type=str, default="resnet50_onnx_model", help="save mm model to this path")
    args.add_argument("--image_dir", "--image_dir",  type=str, default="/data/datasets/imagenet/imagenet2/", help="imagenet val datasets")
    args.add_argument("--label_file", "--label_file",  type=str, default="data/imagenet_1000.txt", help="imagenet val label txt")
    args.add_argument("--quant_mode", "--quant_mode", type=str, default="qint8_mixed_float16", help="qint8_mixed_float16, force_float32, force_float16")
    args.add_argument("--shape_mutable", "--shape_mutable", type=str, default="false", help="whether the mm model is dynamic or static or not")
    args.add_argument('--batch_size', dest = 'batch_size', default = 1, type = int, help = 'batch_size')
    args.add_argument('--input_width', dest = 'input_width', default = 224, type = int, help = 'model input width')
    args.add_argument('--input_height', dest = 'input_height', default = 224, type = int, help = 'model input height')
    args.add_argument('--device_id', dest = 'device_id', default = 0, type = int, help = 'device_id')
    args = args.parse_args()
    supported_quant_mode = ['qint8_mixed_float16', 'qint8_mixed_float32', 'force_float16', 'force_float32']
    if args.quant_mode not in supported_quant_mode:
        print('quant_mode [' + args.quant_mode + ']', 'not supported')
        exit()
 
    network = onnx_parser(args)
    config = generate_model_config(args)
    if args.quant_mode.find('qint') != -1:
        print('do calibrate...')
        calibrate(args, network, config)
    # 生成模型
    print('build model...')
    builder = mm.Builder()
    model = builder.build_model('resnet50_onnx_model', network, config)
    assert model is not None
    # 将模型序列化为离线文件
    output_mm = "./models/mm_model/resnet50_" + args.quant_mode + "_" + args.shape_mutable + '.mm'
    assert model.serialize_to_file(output_mm).ok()
    print("Generate model done, model save to %s" %output_mm)
if __name__ == "__main__":
    main()
