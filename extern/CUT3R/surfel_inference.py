#!/usr/bin/env python3
"""
3D Point Cloud Inference and Visualization Script

This script performs inference using the ARCroco3DStereo model and visualizes the
resulting 3D point clouds with the PointCloudViewer. Use the command-line arguments
to adjust parameters such as the model checkpoint path, image sequence directory,
image size, device, etc.

Usage:
    python demo_ga.py [--model_path MODEL_PATH] [--seq_path SEQ_PATH] [--size IMG_SIZE]
                            [--device DEVICE] [--vis_threshold VIS_THRESHOLD] [--output_dir OUT_DIR]

Example:
    python demo_ga.py --model_path src/cut3r_512_dpt_4_64.pth \
        --seq_path examples/001 --device cuda --size 512
"""

import os
import numpy as np
import torch
import time
import glob
import random
import cv2
import argparse
import tempfile
import shutil
from copy import deepcopy
from add_ckpt_path import add_path_to_dust3r
import imageio.v2 as iio

# Set random seed for reproducibility.
random.seed(42)



def listify(elems):
    return [x for e in elems for x in e]


def collate_with_cat(whatever, lists=False):
    if isinstance(whatever, dict):
        return {k: collate_with_cat(vals, lists=lists) for k, vals in whatever.items()}

    elif isinstance(whatever, (tuple, list)):
        if len(whatever) == 0:
            return whatever
        elem = whatever[0]
        T = type(whatever)

        if elem is None:
            return None
        if isinstance(elem, (bool, float, int, str)):
            return whatever
        if isinstance(elem, tuple):
            return T(collate_with_cat(x, lists=lists) for x in zip(*whatever))
        if isinstance(elem, dict):
            return {
                k: collate_with_cat([e[k] for e in whatever], lists=lists) for k in elem
            }

        if isinstance(elem, torch.Tensor):
            return listify(whatever) if lists else torch.cat(whatever)
        if isinstance(elem, np.ndarray):
            return (
                listify(whatever)
                if lists
                else torch.cat([torch.from_numpy(x) for x in whatever])
            )

        # otherwise, we just chain lists
        return sum(whatever, T())


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run 3D point cloud inference and visualization using ARCroco3DStereo."
    )
    parser.add_argument(
        "--model_path",
        type=str,
        default="src/cut3r_512_dpt_4_64.pth",
        help="Path to the pretrained model checkpoint.",
    )
    parser.add_argument(
        "--seq_path",
        type=str,
        default="",
        help="Path to the directory containing the image sequence.",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="cuda",
        help="Device to run inference on (e.g., 'cuda' or 'cpu').",
    )
    parser.add_argument(
        "--size",
        type=int,
        default="512",
        help="Shape that input images will be rescaled to; if using 224+linear model, choose 224 otherwise 512",
    )
    parser.add_argument(
        "--vis_threshold",
        type=float,
        default=1.5,
        help="Visualization threshold for the point cloud viewer. Ranging from 1 to INF",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./demo_tmp",
        help="value for tempfile.tempdir",
    )

    return parser.parse_args()


def prepare_input(
    img_paths, img_mask, size, raymaps=None, raymap_mask=None, revisit=1, update=True
):
    """
    Prepare input views for inference from a list of image paths.

    Args:
        img_paths (list): List of image file paths.
        img_mask (list of bool): Flags indicating valid images.
        size (int): Target image size.
        raymaps (list, optional): List of ray maps.
        raymap_mask (list, optional): Flags indicating valid ray maps.
        revisit (int): How many times to revisit each view.
        update (bool): Whether to update the state on revisits.

    Returns:
        list: A list of view dictionaries.
    """
    # Import image loader (delayed import needed after adding ckpt path).
    from src.dust3r.utils.image import load_images

    images = load_images(img_paths, size=size)
    num_views = len(images)
    views = []

    for i in range(num_views):
        
        view = {
        "img": images[i]["img"],
        "ray_map": torch.full(
                (
                    images[i]["img"].shape[0],
                    images[i]["img"].shape[-2],
                    images[i]["img"].shape[-1],
                    6,
                ),
                torch.nan,
            ),
            "true_shape": torch.from_numpy(images[i]["true_shape"]),
            "idx": i,
            "instance": str(i),
            "camera_pose": torch.from_numpy(np.eye(4).astype(np.float32)).unsqueeze(
                0
            ),
            "img_mask": torch.tensor(True).unsqueeze(0),
            "ray_mask": torch.tensor(False).unsqueeze(0),
            "update": torch.tensor(True).unsqueeze(0),
            "reset": torch.tensor(False).unsqueeze(0),
        }
        views.append(view)
    return views


def prepare_output(output, poses, depths, lr, niter, outdir, device, save_flag=False):
    from cloud_opt.dust3r_opt import global_aligner, GlobalAlignerMode

    with torch.enable_grad():
        mode = GlobalAlignerMode.PointCloudOptimizer
        
        scene = global_aligner(
            output,
            device=device,
            mode=mode,
            verbose=True,
        )
        if depths is not None:
            scene.preset_depth(depths)
        if poses is not None:
            scene.preset_pose(poses)
        
        loss = scene.compute_global_alignment(
            init="mst",
            niter=niter,
            schedule="linear",
            lr=lr,
        )
    scene.clean_pointcloud()
    pts3d = scene.get_pts3d()
    depths = scene.get_depthmaps()
    poses = scene.get_im_poses()
    focals = scene.get_focals()
    pps = scene.get_principal_points()
    confs = scene.get_conf(mode="none")

    pts3ds_other = [pts.detach().cpu().unsqueeze(0) for pts in pts3d]
    depths = [d.detach().cpu().unsqueeze(0) for d in depths]
    colors = [torch.from_numpy(img).unsqueeze(0) for img in scene.imgs]
    confs = [conf.detach().cpu().unsqueeze(0) for conf in confs]
    cam_dict = {
        "focal": focals.detach().cpu().numpy(),
        "pp": pps.detach().cpu().numpy(),
        "R": poses.detach().cpu().numpy()[..., :3, :3],
        "t": poses.detach().cpu().numpy()[..., :3, 3],
    }
    if save_flag:
        depths_tosave = torch.cat(depths)  # B, H, W
        pts3ds_other_tosave = torch.cat(pts3ds_other)  # B, H, W, 3
        conf_self_tosave = torch.cat(confs)  # B, H, W
        colors_tosave = torch.cat(colors)  # [B, H, W, 3]
        cam2world_tosave = poses.detach().cpu()  # B, 4, 4
        intrinsics_tosave = (
            torch.eye(3).unsqueeze(0).repeat(cam2world_tosave.shape[0], 1, 1)
        )  # B, 3, 3
        intrinsics_tosave[:, 0, 0] = focals[:, 0].detach().cpu()
        intrinsics_tosave[:, 1, 1] = focals[:, 0].detach().cpu()
        intrinsics_tosave[:, 0, 2] = pps[:, 0].detach().cpu()
        intrinsics_tosave[:, 1, 2] = pps[:, 1].detach().cpu()

    
        os.makedirs(os.path.join(outdir, "depth"), exist_ok=True)
        os.makedirs(os.path.join(outdir, "conf"), exist_ok=True)
        os.makedirs(os.path.join(outdir, "color"), exist_ok=True)
        os.makedirs(os.path.join(outdir, "camera"), exist_ok=True)
        
        for f_id in range(len(depths_tosave)):
            depth = depths_tosave[f_id].cpu().numpy()
            conf = conf_self_tosave[f_id].cpu().numpy()
            color = colors_tosave[f_id].cpu().numpy()
            c2w = cam2world_tosave[f_id].cpu().numpy()
            intrins = intrinsics_tosave[f_id].cpu().numpy()
            np.save(os.path.join(outdir, "depth", f"{f_id:06d}.npy"), depth)
            np.save(os.path.join(outdir, "conf", f"{f_id:06d}.npy"), conf)
            iio.imwrite(
                os.path.join(outdir, "color", f"{f_id:06d}.png"),
                (color * 255).astype(np.uint8),
            )
            np.savez(
                os.path.join(outdir, "camera", f"{f_id:06d}.npz"),
                pose=c2w,
                intrinsics=intrins,
            )

    return pts3ds_other, colors, depths, confs, cam_dict


def parse_seq_path(p):
    if os.path.isdir(p):
        img_paths = sorted(glob.glob(f"{p}/*"))
        tmpdirname = None
    else:
        cap = cv2.VideoCapture(p)
        if not cap.isOpened():
            raise ValueError(f"Error opening video file {p}")
        video_fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if video_fps == 0:
            cap.release()
            raise ValueError(f"Error: Video FPS is 0 for {p}")
        frame_interval = 1
        frame_indices = list(range(0, total_frames, frame_interval))
        print(
            f" - Video FPS: {video_fps}, Frame Interval: {frame_interval}, Total Frames to Read: {len(frame_indices)}"
        )
        img_paths = []
        tmpdirname = tempfile.mkdtemp()
        for i in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ret, frame = cap.read()
            if not ret:
                break
            frame_path = os.path.join(tmpdirname, f"frame_{i}.jpg")
            cv2.imwrite(frame_path, frame)
            img_paths.append(frame_path)
        cap.release()
    return img_paths, tmpdirname




def run_inference_from_pil(
    pil_images, 
    model,
    poses=None,
    depths=None,
    lr = 0.01,
    niter = 300,
    device="cuda", 
    size=512, 
    output_dir="./demo_tmp", 
    visualize=False, 
    vis_threshold=1.5,
    save_flag=False
):
    """
    Run 3D reconstruction from a list of PIL images.
    
    Args:
        pil_images (list): List of PIL image objects.
        poses (list): List of camera poses.
        model_path (str): Path to the pretrained model checkpoint.
        device (str): Device to run inference on ('cuda' or 'cpu').
        size (int): Target image size for processing.
        output_dir (str): Directory to save outputs.
        visualize (bool): Whether to launch the point cloud viewer.
        vis_threshold (float): Visualization threshold for point cloud viewer.
        
    Returns:
        dict: A dictionary containing the reconstruction results:
            - point_clouds: List of point cloud tensors
            - colors: List of color tensors
            - confidences: List of confidence tensors
            - camera_info: Camera parameters dictionary
    """
    # Set up the computation device
    if device == "cuda" and not torch.cuda.is_available():
        print("CUDA not available. Switching to CPU.")
        device = "cpu"

    # Add the checkpoint path (required for model imports in the dust3r package)
    

    # Import model and inference functions after adding the ckpt path
    from src.dust3r.inference import inference, inference_recurrent
 
    
    # Prepare input views directly from PIL images
    print(f"Processing {len(pil_images)} images...")
    views = prepare_input_from_pil(
        pil_images=pil_images,
        size=size,
        revisit=1,
        update=True,
    )

    # Run inference
    print("Running inference...")
    start_time = time.time()
    output = {
        "view1": [],
        "view2": [],
        "pred1": [],
        "pred2": [],
    }
    edges = []

    outputs, state_args = inference(views, model, device)
    for view_id in range(1, len(outputs["views"])):
        output["view1"].append(outputs["views"][0])
        output["view2"].append(outputs["views"][view_id])
        output["pred1"].append(outputs["pred"][0])
        output["pred2"].append(outputs["pred"][view_id])

        edges.append((outputs["views"][0]["idx"], outputs["views"][view_id]["idx"]))

    list_of_tuples = edges
    sorted_indices = sorted(
        range(len(list_of_tuples)),
        key=lambda x: (
            list_of_tuples[x][0] > list_of_tuples[x][1],  # Grouping condition
            (
                list_of_tuples[x][1]
                if list_of_tuples[x][0] > list_of_tuples[x][1]
                else list_of_tuples[x][0]
            ),  # First sort key
            (
                list_of_tuples[x][0]
                if list_of_tuples[x][0] > list_of_tuples[x][1]
                else list_of_tuples[x][1]
            ),  # Second sort key
        ),
    )
    
    new_output = {
        "view1": [],
        "view2": [],
        "pred1": [],
        "pred2": [],
    }
    for i in sorted_indices:
        new_output["view1"].append(output["view1"][i])
        new_output["view2"].append(output["view2"][i])
        new_output["pred1"].append(output["pred1"][i])
        new_output["pred2"].append(output["pred2"][i])
        
    output["view1"] = collate_with_cat(new_output["view1"])
    output["view2"] = collate_with_cat(new_output["view2"])
    output["pred1"] = collate_with_cat(new_output["pred1"])
    output["pred2"] = collate_with_cat(new_output["pred2"])

    total_time = time.time() - start_time
    per_frame_time = total_time / len(views)
    print(f"Inference completed in {total_time:.2f} seconds (average {per_frame_time:.2f} s per frame).")

    # Process outputs
    print("Processing reconstruction output...")
    pts3ds_other, colors, depths, conf, cam_dict = prepare_output(output, poses, depths, lr, niter, output_dir, device, save_flag)
    
    # Create result dictionary
    result = {
        "point_clouds": pts3ds_other,
        "colors": colors,
        "depths": depths,
        "confidences": conf,
        "camera_info": cam_dict
    }

    # Visualize if requested
    if visualize:
        from viser_utils import PointCloudViewer
        
        # Convert tensors to numpy arrays for visualization
        pts3ds_to_vis = [p.cpu().numpy() for p in pts3ds_other]
        colors_to_vis = [c.cpu().numpy() for c in colors]
        edge_colors = [None] * len(pts3ds_to_vis)

        # Create and run the point cloud viewer
        print("Launching point cloud viewer...")
        viewer = PointCloudViewer(
            model,
            state_args,
            pts3ds_to_vis,
            colors_to_vis,
            conf,
            cam_dict,
            device=device,
            edge_color_list=edge_colors,
            show_camera=True,
            vis_threshold=vis_threshold,
            size=size,
        )
        viewer.run()
    
    return result


def prepare_input_from_pil(
    pil_images, size, square_ok=False, raymaps=None, raymap_mask=None, revisit=1, update=True
):
    """
    Prepare input views for inference from a list of PIL images.

    Args:
        pil_images (list): List of PIL image objects.
        size (int): Target image size.
        raymaps (list, optional): List of ray maps.
        raymap_mask (list, optional): Flags indicating valid ray maps.
        revisit (int): How many times to revisit each view.
        update (bool): Whether to update the state on revisits.

    Returns:
        list: A list of view dictionaries.
    """
    # Import needed utilities (delayed import needed after adding ckpt path)
    from src.dust3r.utils.image import _resize_pil_image, ImgNorm, exif_transpose
    import PIL
    
    # Process PIL images to have the same format as the load_images output
    imgs = []
    for i, img in enumerate(pil_images):
        # Convert to RGB to ensure consistency
        img = exif_transpose(img).convert("RGB")
        W1, H1 = img.size
        if size == 224:

            img = _resize_pil_image(img, round(size * max(W1 / H1, H1 / W1)))
        else:

            img = _resize_pil_image(img, size)
        W, H = img.size
        cx, cy = W // 2, H // 2
        if size == 224:
            half = min(cx, cy)
            img = img.crop((cx - half, cy - half, cx + half, cy + half))
        else:
            halfw, halfh = ((2 * cx) // 16) * 8, ((2 * cy) // 16) * 8
            if not (square_ok) and W == H:
                halfh = 3 * halfw / 4
            img = img.crop((cx - halfw, cy - halfh, cx + halfw, cy + halfh))

        
        # Create dictionary with the same structure as in load_images
        imgs.append({
            "img": ImgNorm(img)[None],  # Using ImgNorm for normalization
            "true_shape": np.int32([img.size[::-1]]),
            "idx": i,
            "instance": str(i),
        })
    
    # Prepare views similar to prepare_input
    views = []
    num_views = len(imgs)
    
    for i in range(num_views):
        view = {
            "img": imgs[i]["img"],
            "ray_map": torch.full(
                (
                    imgs[i]["img"].shape[0],
                    imgs[i]["img"].shape[-2],
                    imgs[i]["img"].shape[-1],
                    6,
                ),
                torch.nan,
            ),
            "true_shape": torch.from_numpy(imgs[i]["true_shape"]),
            "idx": i,
            "instance": str(i),
            "camera_pose": torch.from_numpy(np.eye(4).astype(np.float32)).unsqueeze(0),
            "img_mask": torch.tensor(True).unsqueeze(0),
            "ray_mask": torch.tensor(False).unsqueeze(0),
            "update": torch.tensor(True).unsqueeze(0),
            "reset": torch.tensor(False).unsqueeze(0),
        }
        views.append(view)
    
    return views




