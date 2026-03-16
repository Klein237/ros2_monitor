"""
node_monitor.launch.py

Lance le NodeMonitor avec sa configuration YAML.

Usage :
  ros2 launch ros2_monitor node_monitor.launch.py
  ros2 launch ros2_monitor node_monitor.launch.py config_file:=/path/to/config.yaml
  ros2 launch ros2_monitor node_monitor.launch.py dry_run:=true
"""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg_share = get_package_share_directory('ros2_monitor')
    default_config = str(Path(pkg_share) / 'config' / 'monitor_config.yaml')

    config_arg = DeclareLaunchArgument(
        'config_file',
        default_value=default_config,
        description='Chemin vers le fichier YAML de configuration',
    )
    poll_arg = DeclareLaunchArgument(
        'poll_interval',
        default_value='3.0',
        description='Intervalle de polling en secondes',
    )
    dry_run_arg = DeclareLaunchArgument(
        'dry_run',
        default_value='false',
        description='Si true, log les actions sans les exécuter',
    )
    fast_poll_arg = DeclareLaunchArgument(
        'fast_poll_interval',
        default_value='0.5',
        description='Intervalle du timer rapide de détection en secondes',
    )
    diag_rate_arg = DeclareLaunchArgument(
        'diag_rate',
        default_value='1.0',
        description='Fréquence de publication sur /diagnostics en Hz',
    )

    monitor_node = Node(
        package='ros2_monitor',
        executable='node_monitor',
        name='node_monitor',
        output='screen',
        parameters=[{
            'config_file':        LaunchConfiguration('config_file'),
            'poll_interval':      LaunchConfiguration('poll_interval'),
            'fast_poll_interval': LaunchConfiguration('fast_poll_interval'),
            'dry_run':            LaunchConfiguration('dry_run'),
            'diag_rate':          LaunchConfiguration('diag_rate'),
        }],
    )

    return LaunchDescription([
        config_arg,
        poll_arg,
        fast_poll_arg,
        dry_run_arg,
        diag_rate_arg,
        monitor_node,
    ])