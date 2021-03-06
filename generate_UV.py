import pdb
import torch
from utils import TrainOptions
# from utils.imutils import save_uv_map, save_Img
from torch.utils.data import DataLoader
from models.uv_generator import Index_UV_Generator
from datasets.base_dataset import BaseDataset
import cv2
import os
import matplotlib.pyplot as plt


def save_uv_map(save_names, save_dir, uv_maps):
    if uv_maps.shape[3] != 3:

        uv_maps = uv_maps.permute(0, 2, 3, 1)

    save_uv_maps = uv_maps.cpu().numpy()

    for i in range(save_uv_maps.shape[0]):
        save_name = os.path.join(save_dir, save_names[i].split('/')[-1])
        if not os.path.exists(save_dir):
            os.mkdir(save_dir)
        cv2.imwrite(save_name, save_uv_maps[i] * 255)


def save_Img(save_names, save_dir, saved_img):
    # if we use img_orig, then we do NOT need to do de normalize.
    # saved_img = saved_img * torch.tensor([0.229, 0.224, 0.225], device=saved_img.device).reshape(1, 3, 1, 1)
    # saved_img = saved_img + torch.tensor([0.485, 0.456, 0.406], device=saved_img.device).reshape(1, 3, 1, 1)

    saved_img = saved_img.permute(0, 2, 3, 1)

    saved_img = saved_img.cpu().numpy()

    for i in range(saved_img.shape[0]):
        save_name = os.path.join(save_dir, save_names[i].split('/')[-1])
        if not os.path.exists(save_dir):
            os.mkdir(save_dir)
        cv2.imwrite(save_name, saved_img[i, :, :, ::-1] * 255)


def warp_feature(dp_out, feature_map, uv_res):
    """
    C: channel number of the input feature map;  H: height;  W: width
    :param dp_out: IUV image in shape (batch_size, 3, H, W)
    :param feature_map: Local feature map in shape (batch_size, C, H, W)
    :param uv_res: The resolution of the transferred feature map in UV space.
    :return: warped_feature: Feature map in UV space with shape (batch_size, C+3, uv_res, uv_res)
    The x, y cordinates in the image sapce and mask will be added as the last 3 channels
     of the warped feature, so the channel number of warped feature is C+3.
    """

    assert dp_out.shape[0] == feature_map.shape[0]
    assert dp_out.shape[2] == feature_map.shape[2]
    assert dp_out.shape[3] == feature_map.shape[3]

    dp_mask = dp_out[:, 0].unsqueeze(1)     # I channel, confidence of being foreground
    dp_uv = dp_out[:, 1:]                   # UV channels, UV coordinates
    thre = 0.5                              # The threshold of foreground and background.
    B, C, H, W = feature_map.shape
    device = feature_map.device

    # Get the sampling index of every pixel in batch_size dimension.
    index_batch = torch.arange(0, B, device=device, dtype=torch.long)[:, None, None].expand([-1, H, W])
    index_batch = index_batch.contiguous().view(-1).long()

    # Get the sampling index of every pixel in H and W dimension.
    tmp_x = torch.arange(0, W, device=device, dtype=torch.long)
    tmp_y = torch.arange(0, H, device=device, dtype=torch.long)

    y, x = torch.meshgrid(tmp_y, tmp_x)
    y = y.contiguous().view(-1).repeat([B])
    x = x.contiguous().view(-1).repeat([B])

    # Sample the confidence of every pixel,
    # and only preserve the pixels belong to foreground.
    conf = dp_mask[index_batch, 0, y, x].contiguous()
    valid = conf > thre
    index_batch = index_batch[valid]
    x = x[valid]
    y = y[valid]

    # Sample the uv coordinates of foreground pixels
    uv = dp_uv[index_batch, :, y, x].contiguous()
    num_pixel = uv.shape[0]
    # Get the corresponding location in UV space
    uv = uv * (uv_res - 1)
    uv_round = uv.round().long().clamp(min=0, max=uv_res - 1)

    # We first process the transferred feature in shape (batch_size * H * W, C+3),
    # so we need to get the location of each pixel in the two-dimension feature vector.
    index_uv = (uv_round[:, 1] * uv_res + uv_round[:, 0]).detach() + index_batch * uv_res * uv_res

    # Sample the feature of foreground pixels
    sampled_feature = feature_map[index_batch, :, y, x]
    # Scale x,y coordinates to [-1, 1] and
    # concatenated to the end of sampled feature as extra channels.
    y = (2 * y.float() / (H - 1)) - 1
    x = (2 * x.float() / (W - 1)) - 1
    sampled_feature = torch.cat([sampled_feature, x[:, None], y[:, None]], dim=-1)

    # Multiple pixels in image space may be transferred to the same location in the UV space.
    # warped_w is used to record the number of the pixels transferred to every location.
    warped_w = sampled_feature.new_zeros([B * uv_res * uv_res, 1])
    warped_w.index_add_(0, index_uv, sampled_feature.new_ones([num_pixel, 1]))

    # Transfer the sampled feature to UV space.
    # Feature vectors transferred to the sample location will be accumulated.
    warped_feature = sampled_feature.new_zeros([B * uv_res * uv_res, C + 2])
    warped_feature.index_add_(0, index_uv, sampled_feature)

    # Normalize the accumulated feature with the pixel number.
    warped_feature = warped_feature / (warped_w + 1e-8)
    # Concatenate the mask channel at the end.
    warped_feature = torch.cat([warped_feature, (warped_w > 0).float()], dim=-1)
    # Reshape the shape to (batch_size, C+3, uv_res, uv_res)
    warped_feature = warped_feature.reshape(B, uv_res, uv_res, C + 3).permute(0, 3, 1, 2)

    return warped_feature


def trans_img2UV(options, dataset='3doh'):
    # dataset = BaseDataset(options, dataset, use_augmentation=False, is_train=False, use_IUV=True)
    options.use_augmentation = False
    dataset = BaseDataset(options, dataset, use_augmentation=False, is_train=True, use_IUV=True)

    dataloader = DataLoader(dataset, batch_size=2, shuffle=False)
    item = next(iter(dataloader))
    img, iuv = item['img_orig'], item['gt_iuv']

    dtype = iuv.dtype
    # print(iuv.shape)
    gt_mask_shape = (iuv[:, 0].unsqueeze(1) > 0).type(dtype)
    iuv[:, 1:] = iuv[:, 1:] / 255.0
    gt_uv_shape = iuv[:, 1:]

    dp_out = torch.cat((gt_mask_shape, gt_uv_shape), 1)
    batch_size = img.shape[0]
    # warped_feature = warp_feature(dp_out, img, img.shape[2])
    # use low UV resolution is better for visulization
    warped_feature = warp_feature(dp_out, img, img.shape[2] // 2)
    
    # first 3 channels are transferred pixels
    trans_img, trans_uv = warped_feature[:, :3], warped_feature[:, 3:]
    save_uv_map(item['imgname'], 'examples/BF_UV', trans_uv)
    save_Img(item['imgname'], 'examples/BF_Img', trans_img)
    
    # visualize
    plt.subplot(1, 3, 1)
    plt.imshow(img[0].permute(1, 2, 0))
    plt.subplot(1, 3, 2)
    plt.imshow(iuv[0].permute(1, 2, 0))
    plt.subplot(1, 3, 3)
    plt.imshow(trans_img[0].permute(1, 2, 0))
    plt.savefig('examples/warp_example.png')


if __name__ == '__main__':
    options = TrainOptions().parse_args()
    # trans_img2UV(options, dataset='3doh')
    
    # I do not have the data of 3doh, so I use up-3d train set.
    trans_img2UV(options, dataset='up-3d')

