#!/usr/bin/env python

from __future__ import division
from __future__ import print_function

import math
import os
import Queue
import time
import struct
import threading
import serial
import numpy as np
import numpy.matlib as matlib
import rospy
import rospkg

import actionlib

from std_msgs.msg import String
from sensor_msgs.msg import JointState
from flo_humanoid.msg import MoveAction, MoveResult, MoveFeedback
from read_from_bolide import BolideReader


class BolideController(object):
    """BolideController

    This class interfaces with the bolide hardware. There are a few
    components to make this work.

    Pubishes:
     - joint_states: JointState messages with the current joint state

    Subscribes:
    - motor_commands: Takes a string command of either `halt` or
                      `relax` to halt or relax the motors.

    Actions:
    - move: Takes a list of JointTargets, sends it to the robot
            and begins moving the robot. Feedback is sent back
            but is unreliable for now.

    Parameters:
    - robot_port: The port on which the robot is connected.
                  The default value is `dev/bolide`.
    - robot_joint_config: Where to load the configuration files
    - simulate: Run the node in simulation mode.

    The class loads a config file from somewhere specified in
    flo_humanoid/config/joints or if specified, whatever is in
    robot_joint_config.

    The system expects to get a response from the bolide for every
    packet sent. These aren't really checked right now.
    """

    CMD_version_read = 0x00
    CMD_init_motor = 0x01
    CMD_set_motor = 0x02
    CMD_capture_pos = 0x03
    CMD_relax_motor = 0x04
    CMD_SN_read = 0x05
    CMD_capture_current = 0x06
    CMD_capture_torque = 0x07
    CMD_capture_battery = 0x08
    CMD_SEQ_load_PoseCnt = 0x10
    CMD_SEQ_load_Pose = 0x11
    CMD_SEQ_load_SEQCnt = 0x12
    CMD_SEQ_load_SEQ = 0x13
    CMD_SEQ_loop_load_PoseCnt = 0x14
    CMD_SEQ_relax = 0x20
    CMD_SEQ_halt = 0x30

    NUM_MOTORS = 18

    commands = {'pos': 0x03, 'current': 0x06, 'torque': 0x07}

    feedback = {'error': 0x00, 'keep_going': 0x01,
                'done': 0x02, 'relaxed': 0x04, 'seq_num': 0x11}

    def __init__(self):
        rospy.init_node('robot_manager')  # , log_level=rospy.DEBUG)

        rospack = rospkg.RosPack()

        self.usb_lock = threading.Lock()
        self.port = rospy.get_param('robot_port', '/dev/bolide')
        self.ser = None
        self.simulate = rospy.get_param('simulate', False)
        self.connect()

        self.joint_publisher = rospy.Publisher(
            'joint_states', JointState, queue_size=1)

        rospy.Subscriber(
            'motor_commands', String, self.new_control_command)

        # that false is autostart. It should always be false
        self.server = actionlib.SimpleActionServer(
            'move', MoveAction, None, False)
        # TODO add in an action for relaxing motors?

        if not self.simulate:
            self.reader = BolideReader(self.ser)

        package_path = rospack.get_path('flo_humanoid')
        default_config_fn = os.path.join(package_path, 'config', 'joints')
        config_fn = rospy.get_param('robot_joint_config',
                                    default_config_fn)
        self.joint_config = dict()
        self.available_motor_ids = []
        self.available_motor_names = []

        with open(config_fn) as cfile:
            headings = None
            for row in cfile:
                if not headings:
                    headings = row.split()
                else:
                    new_data = row.split()
                    new_data_dict = {key: value for key, value in
                                     zip(headings, new_data)}
                    this_address = int(new_data_dict['address'])
                    this_name = new_data_dict['name']
                    self.joint_config[this_address] = new_data_dict
                    self.joint_config[this_name] = new_data_dict
                    self.available_motor_ids.append(this_address)
                    self.available_motor_names.append(this_name)

        if self.simulate:
            self.sim_robot_uploaded_commands = Queue.Queue()
            self.sim_current_pose = np.zeros(self.NUM_MOTORS)
            self.sim_motors_stiff = False
            self.sim_seq_poses = np.array([None]*256)
            self.sim_seq_times = np.array([None]*256)
            self.sim_seq_length = 0
            self.sim_num_poses = 0
            self.sim_timer = time.time()
            self.sim_starting_pose = np.zeros(self.NUM_MOTORS)

        self.rate = rospy.Rate(60)
        self.pose_waiting_override_delay = 1
        self.current_positions = None
        self.motors_initialized = False
        self.state = 'available'
        self.last_pos_req = 0
        self.awaiting_pos_resp = False
        self.moving_params = {}
        self.moving = False
        self.moving_params['time_start'] = 0
        self.moving_params['unique_times'] = []
        self.moving_params['completion_times'] = []
        self.ret = ''
        self.seq_num = 0

        self.server.start()
        rospy.loginfo('started action server for humanoid motion')

        self.read_loop()

    def connect(self):
        """connect to the robot

        Will first try to disconect and close the serial connection if it exists
        then re connect.
        """
        with self.usb_lock:
            if not self.simulate:
                self.close_ser()
                try:
                    self.ser = serial.Serial(self.port, 115200, timeout=0.05)
                except serial.SerialException as err:
                    rospy.logerr(
                        'failed to connect to bolide with err: %s', err)
                    return
            rospy.loginfo('connected to robot')

    def cleanup(self):
        rospy.loginfo('shutting down node')
        self.relax_motors()
        self.close_ser()

    def close_ser(self):
        """Close the serial connection in a safeish way, waiting to be given
        access, flushing, then closing."""
        if self.ser:
            with self.usb_lock:
                self.ser.flush()
                self.ser.close()
                rospy.loginfo('closed serial connection')

    def move(self, goal):
        """Send a goal to move to the robot.

        This function just runs through. It handles the processing to figure
        out how moves line up with the timing and then call the upload for
        the sequence.

        Args:
            goal: A Move.action target

        Returns:
        """
        rospy.loginfo('got new movement action command')
        # done = False
        if self.simulate:
            self.moving_params['time_start'] = rospy.get_time()
        self.moving_params['completion_times'] = np.array([])

        # SETUP AND SHIP MOVE TO ROBOT
        moves = goal.targets
        # build unique times, for now, lets work linearly:
        # TODO build extra times in to allow for better return values and stopping mid move
        self.moving_params['unique_times'] = np.array([0])
        for move in moves:
            tct = move.target_completion_time
            self.moving_params['completion_times'] = np.append(
                self.moving_params['completion_times'], tct)
            if tct not in self.moving_params['unique_times']:
                self.moving_params['unique_times'] = np.append(
                    self.moving_params['unique_times'], tct)
        # now we need to fill in the poses with some default values:
        poses = matlib.repmat(self.current_positions,
                              len(self.moving_params['unique_times']), 1)
        # poses[0] = self.current_positions
        current_move_program_id = np.zeros(self.NUM_MOTORS, dtype=np.uintc)
        # we will run through each move and see where it applies
        for move in moves:
            end_time = move.target_completion_time
            end_id = np.where(
                self.moving_params['unique_times'] == end_time)[0][0]
            # each move will only specify a select number of joints that
            # should change, the rest should stay the same
            for command_idx, name in enumerate(move.name):
                motor_id = int(self.joint_config[name]['address'])
                target_position = move.position[command_idx]
                # we need to know where this motors starting position is
                # being defined. The way this works is that motion will
                # start from the last time this joint was defined.
                # if a user wants to set the start position later in time,
                # they should just pass in the prior pose again at the later
                # start time:
                start_id = current_move_program_id[motor_id]
                start_time = self.moving_params['unique_times'][start_id]

                total_time = (end_time-start_time)
                # figure out where this move would fall, because
                # of the way that the unique times works, this may
                # occur over multiple time points
                if total_time <= 0:
                    raise ValueError(
                        'The total time for the move must be greater than zero')
                percents = (
                    self.moving_params['unique_times']-start_time)/total_time
                prior_position = poses[start_id][motor_id]
                raw_target = target_position * int(self.joint_config[motor_id]['inversion'])*1023/(
                    2*math.pi)+int(self.joint_config[motor_id]['neutral'])
                for motion_idx in range(start_id+1, 1+end_id):
                    next_pose = int(round((raw_target - prior_position)
                                          * percents[motion_idx] + prior_position))
                    # we need to tell all future times to use this pose unless we
                    # change it with another move:
                    for p_idx in range(motion_idx, len(poses)):
                        poses[p_idx][motor_id] = next_pose
                current_move_program_id[motor_id] = end_id
        poses = poses[1:]
        self.moving_params['unique_times'] = self.moving_params['unique_times'][1:]
        rospy.loginfo(
            'telling robot to go to: \n%s \nat times: \n%s', poses, self.moving_params['unique_times'])
        self.upload_sequence(poses, self.moving_params['unique_times'])
        self.moving_params['final_goal'] = poses[-1]
        self.seq_num = 0

        rospy.logdebug('exiting move function')

    def error(self, goal, joint_ids=None):
        if joint_ids is None:
            joint_ids = self.available_motor_ids
        err = 0
        for joint_id in joint_ids:
            err += abs(self.current_positions[joint_id]-goal[joint_id])
        return err

    def read_loop(self):
        """If there are motion tasks to do, do those, otherwise, check the
        robot's pose"""
        while not rospy.is_shutdown():
            rospy.logdebug('starting read loop')
            self.read_all()
            if self.server.new_goal:
                self.move(self.server.accept_new_goal())
            # ITERATE GIVE FEEDBACK
            if self.moving:
                # self.get_pose()
                if self.server.is_preempt_requested():
                    # I think that this would stop motion?
                    self.upload_sequence([self.current_positions], [0])
                    result = MoveResult()
                    result.completed = False
                    # self.get_pose()
                    result.positional_error = self.error(
                        self.moving_params['final_goal'])
                    self.server.set_preempted(result, "Movement Preempted")
                    rospy.loginfo('preempted motion')
                    return
                feedback = MoveFeedback()
                feedback.time_elapsed = rospy.get_time() - \
                    self.moving_params['time_start']
                feedback.time_remaining = self.moving_params['unique_times'][-1] - \
                    feedback.time_elapsed
                # if feedback.time_remaining > 0:
                if self.simulate:
                    feedback.move_number = next(
                        idx for idx, value in enumerate(self.moving_params['completion_times']) if value >
                        feedback.time_elapsed)
                else:
                    feedback.move_number = self.seq_num
                # rospy.loginfo('published feedback')
                if feedback.move_number == len(self.moving_params['completion_times']):
                    result = MoveResult()
                    result.completed = True
                    result.positional_error = self.error(
                        self.moving_params['final_goal'])
                    self.server.set_succeeded(result, "Motion complete")
                    self.moving = False
                    rospy.loginfo('completed motion')
                else:
                    self.server.publish_feedback(feedback)
            elif (not self.awaiting_pos_resp
                  or time.time()-self.last_pos_req > self.pose_waiting_override_delay):
                self.request_pos()
            self.rate.sleep()
        self.cleanup()

# TODO: a lot of this could be vectorized using np
    def get_pose_sim(self):
        """get the pose of the robot and publish it to the joint state"""
        # with self.usb_lock:
        ### Start Simulator ###
        if self.moving:
            cur_time = time.time() - self.sim_timer
            if cur_time > self.sim_seq_times[self.sim_seq_length-1]:
                self.moving = False
            else:
                current_move = next(idx for (idx, val) in enumerate(
                    self.sim_seq_times) if val > cur_time)
                if current_move == 0:
                    percent_complete = cur_time/self.sim_seq_times[0]
                    for idx in range(len(self.sim_current_pose)):
                        self.sim_current_pose[idx] = percent_complete * (
                            self.sim_seq_poses[0][idx] - self.sim_starting_pose[idx]) + self.sim_starting_pose[idx]
                else:
                    percent_complete = (cur_time-self.sim_seq_times[current_move-1])/(
                        self.sim_seq_times[current_move]-self.sim_seq_times[current_move-1])
                    for idx in range(len(self.sim_current_pose)):
                        self.sim_current_pose[idx] = (percent_complete * (
                            self.sim_seq_poses[current_move][idx] -
                            self.sim_seq_poses[current_move-1][idx]) +
                            self.sim_seq_poses[current_move-1][idx])

        position = self.sim_current_pose  # if not self.sim_moving else []

        self.process_return(self.commands['pos'], position)
        ### End Simulator ###
        # else:
        # try:
        # position = self.reader.read_data('pos')
        # except serial.SerialException as err:
        # rospy.logerr('error when reading position: %s', err)
        # self.connect()
        # if position is None:
        # rospy.logerr('couldn\'t get postion data')
        # return

    def new_control_command(self, msg):
        """take a new control message and add it to the control queue

        :param msg: the message being given
        """
        if msg.data == "halt":
            self.send_packet([0x30])
        elif msg.data == "relax":
            self.send_packet([0x20])

    def send_packet(self, command):
        """send_packet to the robot, add in the packet header and footer.

        :param command: the command to send, this should be given as a list
        """
        self.read_all()
        returns = False
        with self.usb_lock:
            # self.ser.flushInput()  # TODO I don't like needing this
            to_send = bytearray([0xff, len(command)+3]+command+[0xfe])
            rospy.loginfo('sending: %s', [hex(s) for s in to_send])
            self.ser.write(to_send)
            rospy.loginfo('waiting for response')
            self.state = 'waiting_for_feedback'
            returns = self.read_one()
            rospy.loginfo('received response: %s', returns)
        return returns

        # TODO: make more informative feedback
        # feedback = self.reader.read_feedback(20)
        # rospy.loginfo('feedback: %s', feedback)

    def relax_motors(self):
        """relax_motors"""
        if self.simulate:
            self.sim_motors_stiff = False
        else:
            self.send_packet([self.CMD_SEQ_relax])
        self.motors_initialized = False

    def upload_pose(self, id, pose):
        """upload_pose, send the given pose to the robot to prepare for motion

        :param id: The number of the pose. Supported values are 0-255
        :param pose: The actual Pose, an 18 element array
        """
        command = [None] * (self.NUM_MOTORS*2 + 2)
        command[0] = self.CMD_SEQ_load_Pose
        command[1] = id
        for idx, motor in enumerate(pose):
            bb, lb = struct.pack('>H', motor)
            bb = (ord(bb))
            lb = (ord(lb))
            command[idx*2+2] = bb
            command[idx*2+3] = lb
        self.send_packet(command)

    def initialize_motors(self):
        """initialize_motors on the robot to prepare for motion.
        Must be run before trying to move"""
        if self.simulate:
            self.sim_motors_stiff = True
        else:
            self.send_packet([self.CMD_init_motor, self.NUM_MOTORS])

    def upload_poses(self, poses):
        """upload_poses to the robot for a motion sequence.

        :param poses: The poses to upload. This should be a list of lists,
                      with each inner list of length number of motors.
        """
        if self.simulate:
            self.sim_num_poses = len(poses)
            for idx, pose in enumerate(poses):
                self.sim_seq_poses[idx] = pose
        else:
            self.send_packet([self.CMD_SEQ_load_PoseCnt, len(poses)])
            for idx, pose in enumerate(poses):
                self.upload_pose(idx, pose)

    def upload_sequence(self, poses, times):
        """upload_sequence, uploads the entire set of poses to the robot with
           the times that the poses are supposed to be hit. Once this command
           finishes, the robot will begin moving.

        :param poses: The poses which you want to pass through
        :param times: The times which you want to hit each pose. Note that the
                      space between each pose will be hit in linear fashion.
        """
        if not self.motors_initialized:
            self.initialize_motors()
        self.upload_poses(poses)
        if self.simulate:
            self.sim_seq_length = len(times)
            self.sim_timer = time.time()
            for idx, ttime in enumerate(times):
                self.sim_seq_times[idx] = ttime
            self.sim_starting_pose = self.sim_current_pose
        else:
            self.send_packet([self.CMD_SEQ_load_SEQCnt, len(times)])
            for idx, ttime in enumerate(times):
                # time is in units of 10ms on the robot. But in sec coming in
                # TODO: somewhere there should be a check to make sure time is <10 sec
                prior_time = 0 if idx == 0 else times[idx-1]
                ms_time = (ttime-prior_time)*1000
                bb, lb = struct.pack('>H', ms_time)
                bb = (ord(bb))
                lb = (ord(lb))
                self.send_packet([self.CMD_SEQ_load_SEQ, idx, bb, lb])
        self.moving = True
        self.seq_num = 0

    def read(self):
        # while len(ret) < 1 and tries > 0:
        self.ret = self.ser.read(1)
        # tries -= 1
        # log(1, 'len(ret): {} | ret: {}'.format(len(ret), ret))
        if self.ret:
            header = ord(self.ret[0])
        else:
            # log(3, 'not enough data returned after tries')
            return
        if header != 0xFF:
            # log(3, 'first byte read did not match header: {}'.format(header))
            rospy.logerr('Incorrect first byte')
            self.ret = ''
            return

        if len(self.ret) < 2:
            self.ret = self.ret + self.ser.read(1)
            if len(self.ret) < 2:
                return
        len_bit = ord(self.ret[1])

        if len(self.ret) < 3:
            self.ret = self.ret + self.ser.read(1)
            if len(self.ret) < 3:
                return
        command = ord(self.ret[2])

        self.ret = self.ret + self.ser.read(len_bit-len(self.ret))
        if len(self.ret) < len_bit:
            return

        end = ord(self.ret[-1])
        if end != 0xFE:
            # log(3, 'bad end bit')
            rospy.logerr('incorrect end bit received')
            self.ret = ''
            return

        data = self.ret[3:-1]
        self.process_return(command, data)

        return {'command': command, 'data': data}

    def read_all(self):
        returns = []
        if not self.simulate:
            while self.ser.inWaiting():
                ret = self.read()
                if ret:
                    returns.append(ret)
        return returns

    def read_one(self, tries=5):
        while tries:
            while self.ser.inWaiting():
                ret = self.read()
                if ret:
                    return ret
            tries -= 1
            self.rate.sleep()

    def calc_pos(self, data):
        final_joint_pos = [0]*18
        for i in range(18):
            final_joint_pos[i] = (ord(data[2*i]) << 8) + ord(data[2*i + 1])
        # print('{}:{}'.format(com,final_joint_pos))
        return final_joint_pos

    def calc_current(self, data):
        final_joint_pos = [0]*18
        for i in range(18):
            final_joint_pos[i] = (ord(data[2*i]) << 8) + ord(data[2*i + 1])
        final_joint_pos = [fjp / 200.0 for fjp in final_joint_pos]
        # print('{}:{}'.format(com,final_joint_pos))
        return final_joint_pos

    def process_return(self, command, data):
        if command == self.commands['pos']:
            if self.simulate:
                position = data
            else:
                position = self.calc_pos(data)
            rospy.logdebug('raw position data: %s', position)
            names = []
            positions = []
            for id in self.available_motor_ids:
                raw_position = position[id]
                rad_position = (raw_position - int(
                    self.joint_config[id]['neutral'])) * int(
                        self.joint_config[id]['inversion'])*2*math.pi/1023
                names.append(self.joint_config[id]['name'])
                positions.append(rad_position)
            new_msg = JointState()
            new_msg.name = names
            new_msg.position = positions
            new_msg.header.stamp = rospy.Time.now()
            self.joint_publisher.publish(new_msg)
            self.current_positions = position
            self.awaiting_pos_resp = False

        elif command == self.commands['current']:
            current = self.calc_current(data)
        elif command == self.feedback['seq_num']:
            data = ord(data)
            self.seq_num = data + 1
            rospy.loginfo('got seq num: %s', data)
            if data == 0:  # then the motion just started...
                # TODO this is not right, this is actually when move 0 has completed
                self.moving_params['time_start'] = 0

    def process_return_list(self, returns):
        for ret in returns:
            self.process_return(ret['command'], ret['data'])

    def request_pos(self):
        """Send a request to the robot to ask the position of the robot."""
        if self.awaiting_pos_resp:
            rospy.logdebug('requesting position, exiting request unanswered')
        else:
            rospy.logdebug('requesting position, no existing request')
        if self.simulate:
            self.get_pose_sim()
            self.awaiting_pos_resp = False
        else:
            self.ser.write(bytearray([0xFF, 0x04, self.commands['pos'], 0xfe]))
            self.last_pos_req = time.time()
            self.awaiting_pos_resp = True


if __name__ == "__main__":
    CONTROLLER = BolideController()
