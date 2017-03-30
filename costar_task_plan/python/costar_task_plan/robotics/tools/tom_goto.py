#!/usr/bin/env python

# By Chris Paxton
# (c) 2017 The Johns Hopkins University
# See license for more details

import rospy
from costar_robot import InverseKinematicsUR5
from costar_task_plan.robotics.tom.config import TOM_RIGHT_CONFIG as CONFIG

from sensor_msgs.msg import JointState
import tf
import tf_conversions.posemath as pm

def goto(ik, pub, listener, trans, rot): 

  try:
    tbt, tbr = listener.lookupTransform(
            'torso_link',
            'r_base_link',
            rospy.Time(0))

    T_bt = pm.fromTf((tbt, tbr))
    T = pm.toMatrix(pm.fromTf((trans, rot)))

    print trans, rot, tbt, tbr

    Q = ik.findClosestIK(pm.toMatrix(T_bt)*T,
          [-1.0719114121799995, -1.1008140645600006, 1.7366724169200003,
            -0.8972388608399999, 1.25538042294, -0.028902652380000227,])
    print "Closest joints =", Q

    msg = JointState(name=CONFIG['joints'],
                       position=Q)
    pub.publish(msg)
  except  (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException), e:
    pass

if __name__ == '__main__':
  rospy.init_node('tom_simple_goto')

  pub = rospy.Publisher('joint_states_cmd', JointState, queue_size=1000)
  ik = InverseKinematicsUR5()

  """
    position: 
      x: 0.648891402264
      y: -0.563835865845
      z: -0.263676911067
    orientation: 
      x: -0.399888401484
      y: 0.916082302699
      z: -0.0071291983402
      w: -0.0288384391252
  """

  rate = rospy.Rate(30)
  listener = tf.TransformListener()
  try:
    while not rospy.is_shutdown():
      goto(ik, pub, listener, (0.64, -0.56, -0.26), (-0.4, 0.92, -0.01, -0.03))
      rate.sleep()
  except rospy.ROSInterruptException, e:
    pass

