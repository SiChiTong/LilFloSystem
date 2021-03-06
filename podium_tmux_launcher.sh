#!/bin/bash

source ~/.bashrc

tmux new-session -d -s flo
tmux rename-window startup
tmux send-keys 'echo "some blank terminal"' Enter
tmux split-window -t flo -h
tmux send-keys 'connect_to_robot 0 && roscore' Enter

tmux split-window -t flo
tmux send-keys 'connect_to_robot 0 && roslaunch --wait flo_core podium_bringup.launch' Enter # it is just more stable..

tmux split-window -t flo -h
tmux send-keys 'htop' Enter

tmux rotate-window -t flo

tmux split-window -t flo -h
tmux send-keys 'sleep 20 && connect_to_robot 0 && roslaunch --wait flo_telepresence realsense-sp-1.launch platform:=podium' Enter # it is just more stable..

tmux split-window -t flo
tmux send-keys 'sleep 30 && connect_to_robot 0 && roslaunch --wait flo_telepresence realsense-sp-2.launch platform:=podium' Enter # it is just more stable..
