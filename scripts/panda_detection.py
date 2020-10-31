#!/usr/bin/env python

"""
Real-time grasp detection.
"""

import argparse
from pathlib import Path
import time

import cv_bridge
import numpy as np
import rospy
import sensor_msgs.msg
import torch

from vgn import vis
from vgn.detection import *
from vgn.perception import *
from vgn.utils import ros_utils
from vgn.utils.transform import Rotation, Transform


class GraspDetectionServer(object):
    def __init__(self, model_path):
        #  load camera parameters
        self.cam_topic_name = rospy.get_param("~cam/topic_name")
        self.intrinsic = CameraIntrinsic.from_dict(rospy.get_param("~cam/intrinsic"))

        # setup a CV bridge
        self.cv_bridge = cv_bridge.CvBridge()

        # define the worspace
        self.size = 0.3
        self.T_cam_task = Transform(
            Rotation.from_quat([-0.679, 0.726, -0.074, -0.081]), [0.166, 0.101, 0.515]
        )
        self.tf_tree = ros_utils.TransformTree()
        self.tf_tree.broadcast_static(
            self.T_cam_task, "camera_depth_optical_frame", "task"
        )

        # construct the grasp planner object
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.net = load_network(model_path, self.device)

        # initialize the visualization
        vis.clear()
        vis.draw_workspace(0.3)

        # subscribe to the camera
        rospy.Subscriber(
            self.cam_topic_name, sensor_msgs.msg.Image, self.sensor_cb, queue_size=1
        )

        self.last_grasp = None

    def sensor_cb(self, msg):
        # reset tsdf
        self.tsdf = TSDFVolume(0.3, 40)

        # integrate image
        img = self.cv_bridge.imgmsg_to_cv2(msg).astype(np.float32) * 0.001
        self.tsdf.integrate(img, self.intrinsic, self.T_cam_task)

        # detect grasps
        tsdf_vol = self.tsdf.get_grid()
        voxel_size = self.tsdf.voxel_size
        qual_vol, rot_vol, width_vol = predict(tsdf_vol, self.net, self.device)
        qual_vol, rot_vol, width_vol = process(tsdf_vol, qual_vol, rot_vol, width_vol)
        grasps, scores = select(
            qual_vol, rot_vol, width_vol, threshold=0.8, max_filter_size=2
        )
        grasps = [from_voxel_coordinates(g, voxel_size) for g in grasps]

        # draw grasps
        vis.draw_grasps(grasps, scores, 0.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()

    rospy.init_node("panda_grasp")
    GraspDetectionServer(args.model)
    rospy.spin()