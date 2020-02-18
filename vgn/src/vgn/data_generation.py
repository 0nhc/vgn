import uuid
import logging

import numpy as np
import open3d as o3d
import scipy.signal as signal
from tqdm import tqdm

from vgn.hand import Hand
from vgn.grasp import Grasp, Label, to_voxel_coordinates
from vgn.perception.exploration import sample_hemisphere
from vgn.perception.integration import TSDFVolume
from vgn.simulation import GraspExperiment
from vgn.utils.transform import Rotation, Transform


def generate_samples(
    urdf_root,
    hand_config,
    object_set,
    num_scenes,
    num_grasps_per_scene,
    root_dir,
    sim_gui,
    rtf,
    rank,
):
    hand = Hand.from_dict(hand_config)
    size = hand.max_gripper_width * 4
    resolution = 40
    high_resolution = 160

    sim = GraspExperiment(urdf_root, hand, size, sim_gui, rtf)

    if rank == 0:
        root_dir.mkdir(parents=True, exist_ok=True)

    for _ in tqdm(range(num_scenes), disable=rank is not 0):
        # Setup experiment
        sim.setup(object_set)
        sim.save_state()

        # Reconstruct scene
        tsdf = TSDFVolume(size, resolution)
        high_res_tsdf = TSDFVolume(size, high_resolution)

        expected_num_of_viewpoints = 8
        num_viewpoints = np.random.poisson(expected_num_of_viewpoints - 1) + 1

        intrinsic = sim.camera.intrinsic
        extrinsics = sample_hemisphere(size, num_viewpoints)

        for extrinsic in extrinsics:
            depth_img = sim.camera.render(extrinsic)[1]
            tsdf.integrate(depth_img, intrinsic, extrinsic)
            high_res_tsdf.integrate(depth_img, intrinsic, extrinsic)

        point_cloud = high_res_tsdf.extract_point_cloud()
        # o3d.visualization.draw_geometries([point_cloud])

        if point_cloud.is_empty():
            logging.warning("Empty point cloud, skipping scene")
            continue

        # Sample and evaluate grasp candidates
        grasps, labels = [], []

        is_positive = lambda o: o == Label.SUCCESS
        num_negatives = 0

        while len(grasps) < num_grasps_per_scene:
            point, normal = sample_grasp_point(point_cloud, hand.finger_depth)
            grasp, label = evaluate_grasp_point(sim, point, normal)
            if is_positive(label) or num_negatives < num_grasps_per_scene // 2:
                grasps.append(grasp)
                labels.append(label)
                num_negatives += not is_positive(label)

        # Store the sample
        path = root_dir / str(uuid.uuid4().hex)
        store_sample(path, tsdf, grasps, labels)


def sample_grasp_point(point_cloud, finger_depth):

    points = np.asarray(point_cloud.points)
    normals = np.asarray(point_cloud.normals)

    idx = np.random.randint(len(points))
    point, normal = points[idx], normals[idx]

    eps = 0.2
    grasp_depth = np.random.uniform(-eps * finger_depth, (1.0 + eps) * finger_depth)

    point = point + normal * (finger_depth - grasp_depth)

    return point, normal


def evaluate_grasp_point(sim, pos, normal, num_rotations=12):
    # Define initial grasp frame on object surface
    z_axis = -normal
    x_axis = np.r_[1.0, 0.0, 0.0]
    if np.isclose(np.abs(np.dot(x_axis, z_axis)), 1.0, 1e-4):
        x_axis = np.r_[0.0, 1.0, 0.0]
    y_axis = np.cross(z_axis, x_axis)
    x_axis = np.cross(y_axis, z_axis)
    R = Rotation.from_dcm(np.vstack((x_axis, y_axis, z_axis)).T)

    # Try to grasp with different yaw angles
    yaws = np.linspace(0.0, np.pi, num_rotations)
    outcomes, widths = [], []
    for yaw in yaws:
        ori = R * Rotation.from_euler("z", yaw)
        sim.restore_state()
        outcome, width = sim.test_grasp(Transform(ori, pos))

        outcomes.append(outcome)
        widths.append(width)

    # Detect mid-point of widest peak of successful yaw angles
    successes = (np.asarray(outcomes) == Label.SUCCESS).astype(float)
    if np.sum(successes):
        peaks, properties = signal.find_peaks(
            x=np.r_[0, successes, 0], height=1, width=1
        )
        idx_of_widest_peak = peaks[np.argmax(properties["widths"])] - 1
        ori = R * Rotation.from_euler("z", yaws[idx_of_widest_peak])
        width = widths[idx_of_widest_peak]
    else:
        ori = Rotation.identity()
        width = 0.0

    return Grasp(Transform(ori, pos), width), int(np.max(outcomes))


def label2quality(label):
    quality = 1.0 if label == Label.SUCCESS else 0.0
    return quality


def store_sample(path, tsdf, grasps, labels):
    tsdf_vol = tsdf.get_volume()
    shape = tsdf_vol.shape

    tsdf_vol = np.expand_dims(tsdf_vol, 0)
    qual_vol = np.zeros_like(tsdf_vol, dtype=np.float32)
    rot_vol = (
        np.zeros((4,) + shape, dtype=np.float32),
        np.zeros((4,) + shape, dtype=np.float32),
    )
    width_vol = np.zeros_like(tsdf_vol, dtype=np.float32)
    mask = np.zeros_like(tsdf_vol, dtype=np.float32)

    R = Rotation.from_rotvec(np.pi * np.r_[0.0, 0.0, 1.0])

    for grasp, label in zip(grasps, labels):
        grasp = to_voxel_coordinates(grasp, Transform.identity(), tsdf.voxel_size)

        index = np.round(grasp.pose.translation).astype(np.int)
        if np.any(index < 0) or np.any(index > tsdf.resolution - 1):
            continue
        i, j, k = index

        qual_vol[0, i, j, k] = label2quality(label)
        rot_vol[0][:, i, j, k] = grasp.pose.rotation.as_quat()
        rot_vol[1][:, i, j, k] = (grasp.pose.rotation * R).as_quat()
        width_vol[0, i, j, k] = grasp.width
        mask[0, i, j, k] = 1.0

    np.savez_compressed(
        path,
        tsdf_vol=tsdf_vol,
        qual_vol=qual_vol,
        rot_vol=rot_vol,
        width_vol=width_vol,
        mask=mask,
    )