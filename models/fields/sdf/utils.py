"""
@file   utils.py
@author Jianfei Guo, Shanghai AI Lab
@brief  Utility funcitons for SDF model.
"""

__all__ = [
    'idr_geometric_init', 
    'pretrain_sdf_sphere', 
    'pretrain_sdf_capsule', 
    'pretrain_sdf_road_surface'
]

import numpy as np
from tqdm import tqdm
from typing import Literal

import torch
import torch.nn as nn
from torch import optim
import torch.nn.functional as F
from torch.cuda.amp.grad_scaler import GradScaler

from nr3d_lib.logger import Logger
from nr3d_lib.models.blocks.blocks import FCBlock
from nr3d_lib.models.loss.safe import safe_mse_loss
from nr3d_lib.utils import check_to_torch, tensor_statistics

@torch.no_grad()
def idr_geometric_init(decoder: FCBlock, *, radius_init: float, n_embed: int, inside_out=False):
    # Set the decoder's layers weight
    for l, layer in enumerate(decoder.layers):
        if l == decoder.D:
            if not inside_out:
                nn.init.normal_(layer.weight, mean=np.sqrt(np.pi) / np.sqrt(layer.in_features), std=0.0001)
                nn.init.constant_(layer.bias, -1 * radius_init)
            else:
                nn.init.normal_(layer.weight, mean=-np.sqrt(np.pi) / np.sqrt(layer.in_features), std=0.0001)
                nn.init.constant_(layer.bias, radius_init)
        elif l == 0:
            nn.init.zeros_(layer.bias)
            nn.init.zeros_(layer.weight)
            # NOTE: Concat order: [grid_feature, embed_x]
            #       The first 3 dim of embed_x is original x input.
            if n_embed == 3:
                nn.init.normal_(layer.weight[:, -n_embed:], mean=0., std=np.sqrt(2) / np.sqrt(layer.out_features))
            else:
                nn.init.normal_(layer.weight[:, -n_embed:-n_embed+3], mean=0., std=np.sqrt(2) / np.sqrt(layer.out_features))
        else:
            nn.init.zeros_(layer.bias)
            nn.init.normal_(layer.weight, mean=0.0, std=np.sqrt(2) / np.sqrt(layer.out_features))

def pretrain_sdf_sphere(
    implicit_surface: nn.Module, 
    # Training configs
    num_iters=5000, num_points=5000, lr=1.0e-4, w_eikonal=1.0e-3, safe_mse = True, clip_grad_val: float = 0.1, 
    # Shape configs
    target_radius=0.5, target_origin=None, bounding_size=2.0, aabb=None, inside_out=False, 
    # Debug & logging related
    logger: Logger=None, log_prefix: str=None, debug_param_detail=False):
    """
    Pretrain sdf to be a sphere
    """
    device = next(implicit_surface.parameters()).device

    if hasattr(implicit_surface, 'preprocess_per_train_step'):
        implicit_surface.preprocess_per_train_step(0) 

    if aabb is None:
        aabb_min = torch.ones([3,], device=device) * (-bounding_size/2.)
        aabb_max = torch.ones([3,], device=device) * (bounding_size/2.)
    else:
        aabb = check_to_torch(aabb).reshape([2,3])
        aabb_min, aabb_max = aabb[0], aabb[1]
    scale = (aabb_max-aabb_min)/2.
    origin = (aabb_max+aabb_min)/2.
    
    if target_origin is None:
        target_origin = (aabb_max+aabb_min)/2.
    else:
        target_origin = check_to_torch(target_origin).reshape([3,])

    if log_prefix is None: 
        log_prefix = implicit_surface.__class__.__name__

    optimizer = optim.Adam(implicit_surface.parameters(), lr=lr)
    scaler = GradScaler(init_scale=128.0)
    if safe_mse:
        loss_eikonal_fn = lambda x: safe_mse_loss(x, x.new_ones(x.shape), reduction='mean', limit=1.0)
    else:
        loss_eikonal_fn = lambda x: F.mse_loss(x, x.new_ones(x.shape), reduction='mean')
    
    with torch.enable_grad():
        with tqdm(range(num_iters), desc=f"=> pretraining {log_prefix}...") as pbar:
            for it in pbar:
                pts_normalized = torch.empty([num_points, 3], dtype=torch.float, device=device).uniform_(-1+1e-6,1-1e-6)
                # pts = pts_normalized * scale + origin
                
                if not inside_out:
                    sdf_gt = pts_normalized.norm(dim=-1) - target_radius
                else:
                    sdf_gt = target_radius - pts_normalized.norm(dim=-1)
                
                pred = implicit_surface.forward_sdf_nablas(pts_normalized)
                pred_sdf = pred['sdf']
                nablas_norm = pred['nablas'].norm(dim=-1)
                loss = F.l1_loss(pred_sdf, sdf_gt, reduction='mean') + w_eikonal * loss_eikonal_fn(nablas_norm)
                
                optimizer.zero_grad()
                
                # loss.backward()
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                if clip_grad_val is not None:
                    torch.nn.utils.clip_grad.clip_grad_value_(implicit_surface.parameters(), clip_grad_val)
                
                # optimizer.step()
                scaler.step(optimizer)
                scaler.update()
                
                pbar.set_postfix(loss=loss.item())
                if logger is not None:
                    logger.add(f"initialize", log_prefix + 'loss', loss.item(), it)
                    logger.add_nested_dict("initialize", log_prefix + 'sdf.', tensor_statistics(pred_sdf), it)
                    logger.add_nested_dict("initialize", log_prefix + 'nablas_norm.', tensor_statistics(nablas_norm), it)
                    if debug_param_detail:
                        logger.add_nested_dict('initialize', log_prefix + 'encoding.', implicit_surface.encoding.stat_param(with_grad=True), it)

def pretrain_sdf_capsule(
    implicit_surface: nn.Module, tracks_in_obj: torch.Tensor, *, 
    # Training configs
    num_iters=5000, num_points=5000, lr=1.0e-4, w_eikonal=1.0e-3, safe_mse = True, clip_grad_val: float = 0.1, 
    # Shape configs
    surface_distance: float = 0.2, # In normalized(net) space.
    # Debug & logging related
    logger: Logger=None, log_prefix: str=None, debug_param_detail=False):
    """
    Pretrain sdf to be a capsule surrounding input track
    """
    device = next(implicit_surface.parameters()).device
    optimizer = optim.Adam(implicit_surface.parameters(), lr=lr)
    scaler = GradScaler(init_scale=128.0)

    if hasattr(implicit_surface, 'preprocess_per_train_step'):
        implicit_surface.preprocess_per_train_step(0)

    if safe_mse:
        loss_eikonal_fn = lambda x: safe_mse_loss(x, x.new_ones(x.shape), reduction='mean', limit=1.0)
    else:
        loss_eikonal_fn = lambda x: F.mse_loss(x, x.new_ones(x.shape), reduction='mean')

    tracks_in_net = implicit_surface.space.normalize_coords(tracks_in_obj)
    
    if log_prefix is None: 
        log_prefix = implicit_surface.__class__.__name__
    
    with torch.enable_grad():
        with tqdm(range(num_iters), desc=f"=> pretraining {log_prefix}...") as pbar:
            for it in pbar:
                samples = torch.empty([num_points,3], dtype=torch.float, device=device).uniform_(-1, 1)
                # [num_points, 1, 3] - [1, num_tracks, 3] = [num_points, num_tracks, 3] -> [num_points, num_tracks] -> [num_samples]
                min_dis = (samples.unsqueeze(-2) - tracks_in_net.unsqueeze(0)).norm(dim=-1).min(dim=-1).values
                sdf_gt = surface_distance - min_dis
                
                pred = implicit_surface.forward_sdf_nablas(samples)
                pred_sdf = pred['sdf']
                nablas_norm = pred['nablas'].norm(dim=-1)
                loss = F.l1_loss(pred_sdf, sdf_gt, reduction='mean') + w_eikonal * loss_eikonal_fn(nablas_norm)
                
                optimizer.zero_grad()
                
                # loss.backward()
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                if clip_grad_val > 0:
                    torch.nn.utils.clip_grad.clip_grad_value_(implicit_surface.parameters(), clip_grad_val)
                
                # optimizer.step()
                scaler.step(optimizer)
                scaler.update()
                
                pbar.set_postfix(loss=loss.item())
                if logger is not None:
                    logger.add(f"initialize", log_prefix + 'loss', loss.item(), it)
                    logger.add_nested_dict("initialize", log_prefix + 'sdf.', tensor_statistics(pred_sdf), it)
                    logger.add_nested_dict("initialize", log_prefix + 'nablas_norm.', tensor_statistics(nablas_norm), it)
                    if debug_param_detail:
                        logger.add_nested_dict('initialize', log_prefix + 'encoding.', implicit_surface.encoding.stat_param(with_grad=True), it)

def pretrain_sdf_road_surface(
    implicit_surface: nn.Module, tracks_in_obj: torch.Tensor, *, 
    # Training configs
    num_iters=5000, num_points=5000, lr=1.0e-4, w_eikonal=1.0e-3, safe_mse = True, clip_grad_val: float = 0.1, 
    # Shape configs
    # e.g. For waymo, +z points to sky, and ego_car is about 0.5m. Hence, floor_dim='z', floor_up_sign=1, ego_height=0.5
    floor_dim: Literal['x','y','z'] = 'z', # The vertical dimension of world
    floor_up_sign: Literal[1, -1]=-1, # [-1] if (-)dim points to sky else [1]
    ego_height: float = 0., # Estimated ego's height from road, in world space
    # Debug & logging related
    logger: Logger=None, log_prefix: str=None, debug_param_detail=False):
    """
    Pretrain sdf to be a road surface
    """
    floor_dim: int = ['x','y','z'].index(floor_dim)
    other_dims = [i for i in range(3) if i != floor_dim]
    
    device = next(implicit_surface.parameters()).device
    optimizer = optim.Adam(implicit_surface.parameters(), lr=lr)
    scaler = GradScaler(init_scale=128.0)
    
    if hasattr(implicit_surface, 'preprocess_per_train_step'):
        implicit_surface.preprocess_per_train_step(0)
    
    if safe_mse:
        loss_eikonal_fn = lambda x: safe_mse_loss(x, x.new_ones(x.shape), reduction='mean', limit=1.0)
    else:
        loss_eikonal_fn = lambda x: F.mse_loss(x, x.new_ones(x.shape), reduction='mean')
    
    tracks_in_net = implicit_surface.space.normalize_coords(tracks_in_obj)
    
    if log_prefix is None: 
        log_prefix = implicit_surface.__class__.__name__
    
    with torch.enable_grad():
        with tqdm(range(num_iters), desc=f"=> pretraining {log_prefix}...") as pbar:
            for it in pbar:
                samples = torch.empty([num_points,3], dtype=torch.float, device=device).uniform_(-1, 1)
                
                # For each sample point, find the track point of the minimum distance (measured in 3D space.)
                # # [num_points, 1, 3] - [1, num_tracks, 3] = [num_points, num_tracks, 3] -> [num_points, num_tracks] -> [num_samples]
                # min_dis_ret = (samples.unsqueeze(-2) - tracks_in_net.unsqueeze(0)).norm(dim=-1).min(dim=-1)
                
                # For each sample point, find the track point of the minimum distance (measure at 2D space. i.e. xoy plane for floor_dim=z)
                # [num_points, 1, 3] - [1, num_tracks, 3] = [num_points, num_tracks, 3] -> [num_points, num_tracks] -> [num_samples]
                min_dis_ret = (samples[..., None, other_dims] - tracks_in_net[None, ..., other_dims]).norm(dim=-1).min(dim=-1)

                # For each sample point, current floor'z coordinate ad floor_dim
                floor_at_in_net = tracks_in_net[min_dis_ret.indices][..., floor_dim] - floor_up_sign * ego_height / implicit_surface.space.scale[floor_dim]
                
                sdf_gt_in_net = floor_up_sign * (samples[..., floor_dim] - floor_at_in_net) 
                
                pred_in_net = implicit_surface.forward_sdf_nablas(samples, nablas_has_grad=True)
                pred_sdf = pred_in_net['sdf']
                nablas_norm = pred_in_net['nablas'].norm(dim=-1)
                loss = F.smooth_l1_loss(pred_in_net['sdf'], sdf_gt_in_net, reduction='mean') + w_eikonal * loss_eikonal_fn(nablas_norm)
                
                optimizer.zero_grad()
                
                # loss.backward()
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                if clip_grad_val > 0:
                    torch.nn.utils.clip_grad.clip_grad_value_(implicit_surface.parameters(), clip_grad_val)
                
                # optimizer.step()
                scaler.step(optimizer)
                scaler.update()
                
                pbar.set_postfix(loss=loss.item())
                if logger is not None:
                    logger.add(f"initialize", log_prefix + 'loss', loss.item(), it)
                    logger.add_nested_dict("initialize", log_prefix + 'sdf.', tensor_statistics(pred_sdf), it)
                    logger.add_nested_dict("initialize", log_prefix + 'nablas_norm.', tensor_statistics(nablas_norm), it)
                    if debug_param_detail:
                        logger.add_nested_dict('initialize', log_prefix + 'encoding.', implicit_surface.encoding.stat_param(with_grad=True), it)