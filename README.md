Continuous Screen Recorder with Instant Replay Feature

This program continuously records the screen in the background, keeping only the last N minutes in memory. This allows the user to retrieve and save these recordings on demand (similar to NVIDIA's Instant Replay feature).

Key Features

Continuous background screen recording.
Utilizes a circular buffer for efficient memory usage.
Ability to specify the buffered recording duration (e.g., 30, 60, 120 seconds or more).
Hotkey support to trigger replay saving.
Multiple options for video quality and format.
Monitors resource consumption (memory, CPU).

Requirements

The program requires the following libraries:
pip install numpy opencv-python mss keyboard psutil
Use code with caution.
How to Use
Basic Usage
python screen_recorder.py
Use code with caution.
Bash
Additional Options
python screen_recorder.py --buffer 120 --fps 15 --quality 95 --output /path/to/output/folder
Use code with caution.
Bash
Where:
--buffer: Buffer duration in seconds (default: 120).
--fps: Frames per second (default: 15).
--quality: Video quality from 0 to 100 (default: 95).
--output: Directory to save video files (default: a replays folder in the current directory).
Hotkeys
Ctrl+Alt+1: Save the last 30 seconds.
Ctrl+Alt+2: Save the last 60 seconds.
Ctrl+Alt+3: Save the last 120 seconds.
Ctrl+Alt+C: Change video codec.
Ctrl+Alt+Q: Stop the program.

License
This program is freely available for use but Mention 
Faisal's work (*/ω＼*)
