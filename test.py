# pybullet test
import pybullet as p
import time
import os.path as osp
import sys
import pybullet_data
import numpy as np

# Pybullet setup
p.connect(p.GUI)
p.setTimeStep(1./240.)
p.setRealTimeSimulation(0)
p.setAdditionalSearchPath(pybullet_data.getDataPath())

while(1):
    print(1)