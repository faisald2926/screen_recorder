#!/usr/bin/env python3

import time
import os
import threading
import queue
import numpy as np
import cv2
import mss
import datetime
from collections import deque
import psutil
import shutil

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

class ScreenRecorder:
    def __init__(self, buffer_seconds=60, fps=10, monitor_num=1, output_dir=None, quality=95, gui_status_callback=None):
        self.buffer_seconds = buffer_seconds
        self.fps = fps
        self.frame_buffer = deque(maxlen=buffer_seconds * fps)
        self.recording = False
        self.lock = threading.Lock()
        
        self.output_dir = output_dir 
        self.quality = quality # How good should the video look? (0-100)
        self.gui_status_callback = gui_status_callback 
        
        self.monitor_num = monitor_num # Which screen are we watching?

        if not self.output_dir: # Gotta have a place to save things!
            err_msg = "CRITICAL INIT ERROR: ScreenRecorder output_dir cannot be None."
            print(err_msg)
            if self.gui_status_callback:
                try: self.gui_status_callback(err_msg, "error")
                except Exception as gui_cb_err: print(f"Error in gui_cb during critical init: {gui_cb_err}")
            raise ValueError(err_msg)
        
        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except OSError as e:
            err_msg = f"CRITICAL INIT ERROR: Could not create/access output directory '{self.output_dir}': {e}"
            print(err_msg) 
            if self.gui_status_callback:
                try: self.gui_status_callback(err_msg, "error")
                except Exception as gui_cb_err: print(f"Error in gui_cb during critical init: {gui_cb_err}")
            raise OSError(err_msg) from e

        # We'll set up the screen grabber (`sct`) inside the capture thread to keep it happy.
        
        self.fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        self.frame_queue = queue.Queue(maxsize=100)
        
        self.frames_captured = 0
        self.frames_dropped = 0
        self.start_time = None
        self.is_saving_replay = False
        self.process = psutil.Process(os.getpid())

    def _log(self, message, level="info"):
        print(f"[{level.upper()}] {message}") 
        if self.gui_status_callback:
            try:
                self.gui_status_callback(message, level) 
            except Exception as e:
                print(f"Error in gui_status_callback: {e}")
    
    def start_recording(self):
        if self.recording:
            self._log("Recording is already in progress.", "warning")
            return
        
        if not self.output_dir or not os.path.isdir(self.output_dir): # Can't record if we don't know where to save!
             self._log(f"Output directory '{self.output_dir}' is not set or not accessible. Cannot start recording.", "error")
             if self.gui_status_callback and hasattr(self.gui_status_callback, '__call__'):
                 try:
                    if isinstance(self.master, tk.Tk) and self.master.winfo_exists():
                         messagebox.showerror("Path Error", f"Output directory '{self.output_dir}' is not accessible. Please check the path in settings.")
                 except Exception:
                     pass
             return

        self.recording = True
        self.start_time = time.time()
        self.frames_captured = 0 
        self.frames_dropped = 0
        
        self.capture_thread = threading.Thread(target=self._capture_screen, daemon=True)
        self.capture_thread.start()
        
        self.process_thread = threading.Thread(target=self._process_frames, daemon=True)
        self.process_thread.start()
        
        self._log(f"Recording started: buffering last {self.buffer_seconds} seconds at {self.fps} FPS.")
    
    def stop_recording(self):
        if not self.recording:
            self._log("Recording is already stopped.", "warning")
            return
        
        self.recording = False
        
        if hasattr(self, 'capture_thread') and self.capture_thread.is_alive():
            self.capture_thread.join(timeout=2.0)
        if hasattr(self, 'process_thread') and self.process_thread.is_alive():
            self.process_thread.join(timeout=2.0)
        
        duration = time.time() - self.start_time if self.start_time else 0
        self._log(f"Recording stopped. Duration: {duration:.2f}s, Frames captured: {self.frames_captured}")
        if self.frames_dropped > 0:
            drop_percentage = (self.frames_dropped / (self.frames_captured + self.frames_dropped) * 100) if (self.frames_captured + self.frames_dropped) > 0 else 0
            self._log(f"Frames dropped: {self.frames_dropped} ({drop_percentage:.2f}%)", "warning")
    
    def _capture_screen(self):
        try:
            sct = mss.mss() # Okay, screen grabber, wake up in your own thread!
            if not sct.monitors:
                self._log("CRITICAL CAPTURE ERROR: No monitors found by mss in capture thread.", "error")
                self.recording = False 
                return

            actual_monitor_num = self.monitor_num
            if actual_monitor_num < 0 or actual_monitor_num >= len(sct.monitors):
                self._log(f"Warning: Invalid monitor number {self.monitor_num}. Defaulting to monitor 1 (or 0 if only 'all').", "warning")
                actual_monitor_num = 1 if len(sct.monitors) > 1 else 0
            
            monitor_to_capture = sct.monitors[actual_monitor_num] # Making sure we pick a real screen.
            self._log(f"Capture thread will use monitor: {monitor_to_capture}", "info")

        except Exception as e:
            self._log(f"CRITICAL CAPTURE ERROR: Failed to initialize mss or select monitor: {e}", "error")
            self.recording = False 
            return

        frame_interval = 1.0 / self.fps
        next_frame_time = time.time()
        
        try:
            while self.recording:
                current_time = time.time()
                
                if current_time >= next_frame_time:
                    try:
                        screenshot = sct.grab(monitor_to_capture) 
                        frame = np.array(screenshot)
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                        timestamp = time.time()
                        
                        self.frame_queue.put((frame, timestamp), block=False)
                        self.frames_captured += 1
                    except queue.Full: # Oops, processing fell behind, a frame was dropped.
                        self.frames_dropped += 1
                    except Exception as e: 
                        self._log(f"Error in capture loop: {e}", "error") 
                    
                    next_frame_time = max(next_frame_time + frame_interval, current_time) # Ensuring we don't fall behind on capturing frames.
                else:
                    sleep_duration = next_frame_time - current_time
                    if sleep_duration > 0.001 : 
                         time.sleep(min(sleep_duration, frame_interval)) 
                    else:
                        time.sleep(0.001)
        finally:
            if 'sct' in locals(): # Check if sct was successfully initialized
                try:
                    sct.close() # Time to let go of the screen resources.
                    self._log("MSS context closed in capture thread.", "info")
                except Exception as e:
                    self._log(f"Error closing MSS context in capture thread: {e}", "warning")
    
    def _process_frames(self):
        while self.recording or not self.frame_queue.empty():
            try:
                frame, timestamp = self.frame_queue.get(timeout=0.5)
                with self.lock:
                    self.frame_buffer.append((frame, timestamp))
                self.frame_queue.task_done()
            except queue.Empty: # Buffer queue is empty for now, just chill.
                pass
            except Exception as e:
                self._log(f"Error processing frame: {e}", "error")

    def save_replay(self, duration_seconds=None, output_path_override=None, codec=None):
        if self.is_saving_replay:
            self._log("Replay saving is already in progress, please wait.", "warning")
            return None
            
        if not self.frame_buffer: # Nothing in the buffer? Nothing to save, then.
            self._log("Buffer is empty, nothing to save.", "warning")
            return None
        
        if not self.output_dir: 
            self._log("Error: Output directory is not set in the recorder. Cannot save replay.", "error")
            return None

        self.is_saving_replay = True
        actual_output_path = None 
        
        try:
            duration_seconds = duration_seconds or self.buffer_seconds
            duration_seconds = min(duration_seconds, self.buffer_seconds)
            
            if output_path_override: 
                actual_output_path = output_path_override
                override_dir = os.path.dirname(actual_output_path)
                if override_dir: 
                    try:
                        os.makedirs(override_dir, exist_ok=True)
                    except OSError as e:
                        self._log(f"Error: Could not create directory for custom output path '{override_dir}': {e}", "error")
                        self.is_saving_replay = False
                        return None
            else: 
                timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                try:
                    os.makedirs(self.output_dir, exist_ok=True) 
                except OSError as e:
                    self._log(f"Error: Could not create or access output directory '{self.output_dir}' before saving: {e}", "error")
                    self.is_saving_replay = False
                    return None
                actual_output_path = os.path.join(self.output_dir, f"screen_replay_{timestamp_str}.mp4")
            
            frames_to_save = []
            with self.lock:
                frames_needed = min(int(duration_seconds * self.fps), len(self.frame_buffer))
                if frames_needed > 0:
                    frames_to_save = list(self.frame_buffer)[-frames_needed:]
            
            if not frames_to_save:
                self._log("No frames to save for the requested duration.", "warning")
                self.is_saving_replay = False
                return None
            
            first_frame_data = frames_to_save[0][0]
            height, width = first_frame_data.shape[:2]
            
            effective_codec = codec or 'mp4v'
            fourcc_val = cv2.VideoWriter_fourcc(*(effective_codec))
            
            video_writer = cv2.VideoWriter(
                actual_output_path, fourcc_val, self.fps, (width, height)
            )
            if not video_writer.isOpened():
                self._log(f"Error: Could not open video writer for path '{actual_output_path}'. Check path and permissions.", "error")
                self.is_saving_replay = False
                return None

            for frame_data, _ in frames_to_save:
                video_writer.write(frame_data)
            
            video_writer.release() # Alright, video file is finished and closed.
            
            if os.path.exists(actual_output_path) and effective_codec.lower() not in ['mp4v', 'x264']:
                if shutil.which("ffmpeg") is None: # If ffmpeg is around, we can make the video even nicer.
                    self._log("ffmpeg not found in system PATH. Skipping ffmpeg re-encode for quality adjustment.", "warning")
                else:
                    try:
                        temp_path = actual_output_path + ".temp.mp4"
                        crf_value = max(0, int((100 - self.quality) / 2)) 
                        ffmpeg_cmd = (
                            f'ffmpeg -i "{actual_output_path}" -c:v libx264 -crf {crf_value} '
                            f'-preset medium "{temp_path}" -y -loglevel error'
                        )
                        self._log(f"Attempting ffmpeg re-encode: {ffmpeg_cmd}")
                        ret_code = os.system(ffmpeg_cmd) 
                        
                        if ret_code == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                            os.replace(temp_path, actual_output_path)
                            self._log(f"ffmpeg re-encode successful for {actual_output_path}")
                        elif os.path.exists(temp_path):
                            os.remove(temp_path)
                            self._log(f"ffmpeg created an empty/invalid file or failed (code: {ret_code}) for {actual_output_path}. Original file kept.", "warning")
                        else: 
                             self._log(f"ffmpeg re-encode failed (code: {ret_code}). FFmpeg command might have issues or paths are incorrect. Original file kept.", "warning")
                    except Exception as e:
                        self._log(f"Warning: Failed to adjust video quality using ffmpeg: {e}", "warning")
            
            self._log(f"Video saved successfully: {actual_output_path}")
            self._log(f"Duration: {len(frames_to_save) / self.fps:.2f} seconds, Frames: {len(frames_to_save)}")
            return actual_output_path
            
        except Exception as e: 
            self._log(f"An unexpected error occurred during save_replay: {e}", "error")
            import traceback
            traceback.print_exc()
            return None
        finally:
            self.is_saving_replay = False
    
    def trigger_replay(self, duration_seconds=None, codec=None):
        if self.is_saving_replay:
            self._log("Replay saving is already in progress, please wait.", "warning")
            return
        if not self.output_dir: 
            self._log("Cannot trigger replay: Output directory not set in recorder.", "error")
            return
            
        save_thread = threading.Thread(
            target=self.save_replay,
            args=(duration_seconds, None, codec), 
            daemon=True
        )
        save_thread.start()
        self._log(f"Triggered replay saving for the last {duration_seconds or self.buffer_seconds} seconds.")
    
    def get_memory_usage(self):
        if not self.frame_buffer:
            return 0.0
        with self.lock:
            if self.frame_buffer:
                sample_frame = self.frame_buffer[0][0]
                frame_size_bytes = sample_frame.nbytes
                total_memory_mb = (frame_size_bytes * len(self.frame_buffer)) / (1024 * 1024)
                return total_memory_mb
            return 0.0
    
    def get_system_resources(self):
        buffer_len = 0
        with self.lock:
            buffer_len = len(self.frame_buffer)

        total_frames_processed = self.frames_captured + self.frames_dropped
        drop_rate_val = (self.frames_dropped / total_frames_processed * 100) if total_frames_processed > 0 else 0

        return {
            'cpu_percent': self.process.cpu_percent(interval=0.01), 
            'memory_percent': self.process.memory_percent(),
            'memory_mb': self.process.memory_info().rss / (1024 * 1024),
            'buffer_memory_mb': self.get_memory_usage(),
            'buffer_frames': buffer_len,
            'max_buffer_frames': self.buffer_seconds * self.fps,
            'frames_captured': self.frames_captured,
            'frames_dropped': self.frames_dropped,
            'drop_rate': drop_rate_val
        }

class ReplayManager:
    def __init__(self, buffer_seconds=60, fps=10, output_dir=None, quality=95, gui_status_callback=None, monitor_num=1):
        if not output_dir:
            err_msg = "FATAL: ReplayManager cannot be initialized without an output_dir."
            print(err_msg)
            if gui_status_callback:
                try: gui_status_callback(err_msg, "error")
                except: pass
            raise ValueError(err_msg)

        self.recorder = ScreenRecorder(
            buffer_seconds, fps, 
            monitor_num=monitor_num,  # Telling the recorder which screen to focus on.
            output_dir=output_dir, 
            quality=quality, 
            gui_status_callback=gui_status_callback
        )
        self.running = False
        self.codecs = {
            'mp4v': 'MPEG-4 (Default)',
            'XVID': 'XVID (Alternative, needs ffmpeg for quality control)',
            'avc1': 'H.264 (High Quality, needs ffmpeg)' 
        }
        self.current_codec = 'mp4v'
        self._gui_status_callback = gui_status_callback 

    def _manager_log(self, message, level="info"):
        print(f"[{level.upper()}] Manager: {message}")
        if self.recorder and hasattr(self.recorder, '_log') and self.recorder.gui_status_callback:
            self.recorder._log(message, level)
        elif self._gui_status_callback: 
            try:
                self._gui_status_callback(message, level)
            except Exception as e:
                print(f"Error in manager's gui_status_callback: {e}")

    def start(self):
        if self.running:
            self._manager_log("Replay manager is already running.", "warning")
            return
        if not self.recorder or not self.recorder.output_dir:
            self._manager_log("Cannot start: Recorder or its output directory is not initialized.", "error")
            return
        if not os.path.isdir(self.recorder.output_dir): # Double-checking the save folder is actually there before we start.
            self._manager_log(f"Cannot start: Output directory '{self.recorder.output_dir}' is not accessible.", "error")
            if self._gui_status_callback and hasattr(self._gui_status_callback, '__call__'):
                try:
                    if isinstance(self.master, tk.Tk) and self.master.winfo_exists():
                        messagebox.showerror("Path Error", f"Output directory '{self.recorder.output_dir}' is not accessible. Please check settings.")
                except Exception:
                    pass
            return
        self.running = True
        self.recorder.start_recording() 

    def stop(self):
        if not self.running:
            self._manager_log("Replay manager is already stopped.", "warning")
            return
        self.running = False
        self.recorder.stop_recording() 
    
    def set_codec(self, codec_key):
        if codec_key in self.codecs:
            self.current_codec = codec_key
            self._manager_log(f"Video codec set to: {self.codecs[self.current_codec]}")
        else:
            self._manager_log(f"Invalid codec key: {codec_key}", "error")

    def trigger_replay(self, duration_seconds=None):
        if not self.recorder or not self.recorder.output_dir: 
            self._manager_log("Cannot trigger replay: Recorder or its output directory not initialized.", "error")
            return
        self.recorder.trigger_replay(duration_seconds, self.current_codec)
    
    def get_system_resources(self):
        if self.recorder:
            return self.recorder.get_system_resources()
        return {}

    def update_settings(self, buffer_seconds, fps, quality, monitor_num):
        if self.running: # Can't change settings while the engine is running!
            self._manager_log("Cannot update settings while recording is active.", "warning")
            return False
        if not self.recorder:
            self._manager_log("Cannot update settings: Recorder not initialized.", "error")
            return False

        self.recorder.buffer_seconds = buffer_seconds
        self.recorder.fps = fps
        self.recorder.frame_buffer = deque(maxlen=buffer_seconds * fps)
        self.recorder.quality = quality
        self.recorder.monitor_num = monitor_num 
        self._manager_log(f"Settings updated: Buffer={buffer_seconds}s, FPS={fps}, Quality={quality}, Monitor={monitor_num}")
        return True

    def set_output_directory(self, new_dir):
        if self.running:
            self._manager_log("Cannot change output directory while recording.", "warning")
            return False
        if not self.recorder:
            self._manager_log("Cannot set output directory: Recorder not initialized.", "error")
            return False
        try:
            if not new_dir: 
                self._manager_log("Error: Output directory cannot be empty.", "error")
                return False
            if os.name == 'nt': 
                drive = os.path.splitdrive(new_dir)[0]
                if drive and not os.path.exists(drive + "\\"): 
                    self._manager_log(f"Error: Drive '{drive}' for output directory '{new_dir}' does not appear to exist.", "error")
                    return False
            
            os.makedirs(new_dir, exist_ok=True)
            self.recorder.output_dir = new_dir 
            self._manager_log(f"Output directory changed to: {new_dir}")
            return True
        except OSError as e:
            self._manager_log(f"OSError setting output directory '{new_dir}': {e}", "error")
            return False
        except Exception as e:
            self._manager_log(f"Unexpected error setting output directory '{new_dir}': {e}", "error")
            return False

class RecorderGUI:
    def __init__(self, master):
        self.master = master
        master.title("Instant Replay Recorder")
        master.geometry("600x700") 

        self.dark_bg = "#2E2E2E"; self.light_fg = "#E0E0E0"; self.button_bg = "#4A4A4A" # Setting up our cool dark mode colors.
        self.entry_bg = "#3E3E3E"; self.accent_color = "#007ACC"
        self.large_font = ("Segoe UI", 14); self.medium_font = ("Segoe UI", 12); self.small_font = ("Segoe UI", 10) # Nice big fonts for easy reading.

        master.configure(bg=self.dark_bg)
        style = ttk.Style(); style.theme_use('clam') # Making things look a bit more modern.
        style.configure("TFrame", background=self.dark_bg)
        style.configure("TLabel", background=self.dark_bg, foreground=self.light_fg, font=self.medium_font)
        style.configure("TButton", padding=8, relief="flat", font=self.medium_font, background=self.button_bg, foreground=self.light_fg)
        style.map("TButton", background=[('active', '#6A6A6A'), ('disabled', '#383838')], foreground=[('disabled', '#777777')])
        style.configure("TEntry", fieldbackground=self.entry_bg, foreground=self.light_fg, insertcolor=self.light_fg, font=self.medium_font, padding=5)
        style.configure("TCombobox", fieldbackground=self.entry_bg, foreground=self.light_fg, selectbackground=self.entry_bg, selectforeground=self.light_fg, font=self.medium_font)
        master.option_add("*TCombobox*Listbox*Background", self.entry_bg)
        master.option_add("*TCombobox*Listbox*Foreground", self.light_fg)
        master.option_add("*TCombobox*Listbox*font", self.medium_font)

        self.default_output_path = r"C:\Users\fysl9\Desktop\screen_recorder\OutputVID" # A sensible default place for our videos.
        try:
            os.makedirs(self.default_output_path, exist_ok=True)
        except Exception as e:
            print(f"CRITICAL GUI INIT: Failed to create default output directory '{self.default_output_path}': {e}")
            pass 

        self.output_dir_var = tk.StringVar(value=self.default_output_path)
        self.buffer_seconds_var = tk.IntVar(value=60)
        self.fps_var = tk.IntVar(value=15)
        self.quality_var = tk.IntVar(value=95)
        self.codec_var = tk.StringVar()
        self.monitor_var = tk.StringVar() 

        self.status_vars = { # Variables to show what's happening (CPU, memory, etc.).
            "cpu": tk.StringVar(value="CPU: --"), "mem_rss": tk.StringVar(value="Mem RSS: -- MB"),
            "buffer_mem": tk.StringVar(value="Buffer Mem: -- MB"),
            "buffer_frames": tk.StringVar(value="Buffer: --/-- frames"),
            "dropped": tk.StringVar(value="Dropped: -- (0.0%)")
        }

        self.monitor_options = {} 
        try:
            with mss.mss() as sct_temp: # Let's quickly ask mss about available screens for the dropdown.
                for i, mon in enumerate(sct_temp.monitors):
                    label = f"Display {i}: {mon['width']}x{mon['height']} @ ({mon['left']},{mon['top']})"
                    if i == 1 and len(sct_temp.monitors) > 1: 
                        label += " (Primary)"
                    elif i == 0 and len(sct_temp.monitors) == 1: 
                        label += " (All Screens)"
                    self.monitor_options[label] = i 
        except Exception as e:
            print(f"Error getting monitor list for GUI: {e}")
            self.monitor_options["Monitor 1 (Default)"] = 1 

        default_monitor_key = None # Trying to pick the main screen by default for the user.
        if self.monitor_options:
            for key, idx in self.monitor_options.items():
                if idx == 1:
                    default_monitor_key = key
                    break
            if not default_monitor_key: 
                default_monitor_key = list(self.monitor_options.keys())[0] 
        
        if default_monitor_key:
            self.monitor_var.set(default_monitor_key)
        else: 
             self.monitor_var.set("Monitor 1 (Default)")
             self.monitor_options["Monitor 1 (Default)"] = 1


        try:
            selected_monitor_idx = self.monitor_options.get(self.monitor_var.get(), 1) 
            self.manager = ReplayManager(
                buffer_seconds=self.buffer_seconds_var.get(),
                fps=self.fps_var.get(),
                output_dir=self.output_dir_var.get(), 
                quality=self.quality_var.get(),
                gui_status_callback=self.log_to_gui,
                monitor_num=selected_monitor_idx # Passing the chosen screen to the recording manager.
            )
            if self.manager.codecs: 
                 self.codec_var.set(self.manager.current_codec)
        except Exception as e: 
            self.log_to_gui(f"FATAL ERROR during startup: {e}. Application may not function.", "error")
            if hasattr(self, 'master') and self.master and self.master.winfo_exists():
                messagebox.showerror("Startup Error", f"Failed to initialize recorder components: {e}\n\nPlease check paths and permissions. The application might not work correctly.")
            self.manager = None 

        main_frame = ttk.Frame(master, padding="15"); main_frame.pack(expand=True, fill=tk.BOTH)
        settings_frame = ttk.LabelFrame(main_frame, text="Settings", padding="10"); settings_frame.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0,10)); settings_frame.columnconfigure(1, weight=1)
        
        ttk.Label(settings_frame, text="Output Dir:").grid(row=0, column=0, sticky="w", pady=3)
        self.output_entry = ttk.Entry(settings_frame, textvariable=self.output_dir_var); self.output_entry.grid(row=0, column=1, sticky="ew", padx=(5,0), pady=3)
        self.browse_btn = ttk.Button(settings_frame, text="Browse", command=self._browse_output_dir); self.browse_btn.grid(row=0, column=2, sticky="e", padx=(5,0), pady=3)
        ttk.Label(settings_frame, text="Buffer (s):").grid(row=1, column=0, sticky="w", pady=3)
        self.buffer_entry = ttk.Entry(settings_frame, textvariable=self.buffer_seconds_var, width=7); self.buffer_entry.grid(row=1, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(settings_frame, text="FPS:").grid(row=2, column=0, sticky="w", pady=3)
        self.fps_entry = ttk.Entry(settings_frame, textvariable=self.fps_var, width=7); self.fps_entry.grid(row=2, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(settings_frame, text="Quality (0-100):").grid(row=3, column=0, sticky="w", pady=3)
        self.quality_entry = ttk.Entry(settings_frame, textvariable=self.quality_var, width=7); self.quality_entry.grid(row=3, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(settings_frame, text="Codec:").grid(row=4, column=0, sticky="w", pady=3)
        self.codec_combobox = ttk.Combobox(settings_frame, textvariable=self.codec_var, state="readonly")
        if self.manager and self.manager.codecs: self.codec_combobox.config(values=list(self.manager.codecs.keys())); self.codec_combobox.set(self.manager.current_codec)
        else: self.codec_combobox.config(values=["mp4v"]); self.codec_combobox.set("mp4v")
        self.codec_combobox.grid(row=4, column=1, columnspan=2, sticky="ew", padx=5, pady=3); self.codec_combobox.bind("<<ComboboxSelected>>", self._on_codec_change)
        ttk.Label(settings_frame, text="Monitor:").grid(row=5, column=0, sticky="w", pady=3)
        self.monitor_combobox = ttk.Combobox(settings_frame, textvariable=self.monitor_var, 
                                             values=list(self.monitor_options.keys()), state="readonly")
        self.monitor_combobox.grid(row=5, column=1, columnspan=2, sticky="ew", padx=5, pady=3)
        self.apply_settings_btn = ttk.Button(settings_frame, text="Apply Settings", command=self._apply_settings); self.apply_settings_btn.grid(row=6, column=0, columnspan=3, pady=(10,0))

        controls_frame = ttk.LabelFrame(main_frame, text="Controls", padding="10"); controls_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=10); controls_frame.columnconfigure(0, weight=1)
        self.start_stop_btn = ttk.Button(controls_frame, text="Start Recording", command=self._toggle_recording, style="Accent.TButton"); style.configure("Accent.TButton", foreground=self.light_fg, background=self.accent_color, font=self.large_font); style.map("Accent.TButton", background=[('active', "#005f9e")]); self.start_stop_btn.grid(row=0, column=0, columnspan=3, sticky="ew", pady=5, ipady=5)
        replay_buttons_frame = ttk.Frame(controls_frame); replay_buttons_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=5); replay_buttons_frame.columnconfigure(0, weight=1); replay_buttons_frame.columnconfigure(1, weight=1); replay_buttons_frame.columnconfigure(2, weight=1)
        self.save_30s_btn = ttk.Button(replay_buttons_frame, text="Save 30s Replay", command=lambda: self._save_replay(30)); self.save_30s_btn.grid(row=0, column=0, sticky="ew", padx=(0,2))
        self.save_60s_btn = ttk.Button(replay_buttons_frame, text="Save 60s Replay", command=lambda: self._save_replay(60)); self.save_60s_btn.grid(row=0, column=1, sticky="ew", padx=2)
        self.save_120s_btn = ttk.Button(replay_buttons_frame, text="Save 120s Replay", command=lambda: self._save_replay(120)); self.save_120s_btn.grid(row=0, column=2, sticky="ew", padx=(2,0))
        status_frame = ttk.LabelFrame(main_frame, text="Status", padding="10"); status_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10); status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.status_vars["cpu"], font=self.small_font).grid(row=0, column=0, sticky="w"); ttk.Label(status_frame, textvariable=self.status_vars["mem_rss"], font=self.small_font).grid(row=1, column=0, sticky="w"); ttk.Label(status_frame, textvariable=self.status_vars["buffer_mem"], font=self.small_font).grid(row=2, column=0, sticky="w"); ttk.Label(status_frame, textvariable=self.status_vars["buffer_frames"], font=self.small_font).grid(row=3, column=0, sticky="w"); ttk.Label(status_frame, textvariable=self.status_vars["dropped"], font=self.small_font).grid(row=4, column=0, sticky="w")
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding="10"); log_frame.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(10,0)); log_frame.columnconfigure(0, weight=1); log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=8, width=70, wrap=tk.WORD, state="disabled", bg=self.entry_bg, fg=self.light_fg, font=self.small_font, relief=tk.FLAT, highlightthickness=0); self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview); log_scrollbar.grid(row=0, column=1, sticky="ns"); self.log_text.config(yscrollcommand=log_scrollbar.set); main_frame.rowconfigure(3, weight=1)

        self._update_ui_state()
        self.status_update_job = None
        master.protocol("WM_DELETE_WINDOW", self._on_closing)
    
    def log_to_gui(self, message, level="info"): # Updating the GUI log safely from other threads.
        if hasattr(self, 'master') and self.master and self.master.winfo_exists():
            self.master.after(0, self._do_log_to_gui, message, level)
        else: 
            print(f"[LOG_FALLBACK] [{level.upper()}] {message}")

    def _do_log_to_gui(self, message, level):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] [{level.upper()}] {message}\n"
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, formatted_message)
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def _browse_output_dir(self):
        if not self.manager: 
            messagebox.showerror("Error", "Recorder components not initialized. Cannot set path.")
            return
        directory = filedialog.askdirectory(initialdir=self.output_dir_var.get())
        if directory:
            self.output_dir_var.set(directory)
            if not self.manager.running:
                if not self.manager.set_output_directory(directory):
                    messagebox.showerror("Path Error", f"Failed to set output directory to: {directory}. Please check path and permissions. See log for details.")

    def _apply_settings(self):
        if not self.manager:
            messagebox.showerror("Error", "Recorder components not initialized. Cannot apply settings.")
            return
        if self.manager.running:
            messagebox.showwarning("Recording Active", "Cannot change settings while recording. Stop recording first.")
            return
        try:
            buffer_s = int(self.buffer_seconds_var.get())
            fps_val = int(self.fps_var.get())
            quality_val = int(self.quality_var.get())
            output_dir = self.output_dir_var.get()
            selected_monitor_key = self.monitor_var.get()
            monitor_idx = self.monitor_options.get(selected_monitor_key, 1) # Getting the screen number from the user's dropdown choice.

            if not output_dir: 
                messagebox.showerror("Invalid Settings", "Output directory cannot be empty.")
                return
            if buffer_s <=0 or fps_val <=0 or not (0 <= quality_val <= 100):
                messagebox.showerror("Invalid Settings", "Buffer & FPS must be positive. Quality 0-100.")
                return

            all_ok = True
            if not self.manager.update_settings(buffer_s, fps_val, quality_val, monitor_idx): all_ok = False
            if not self.manager.set_output_directory(output_dir):
                all_ok = False
                messagebox.showerror("Path Error", f"Failed to set output directory to: {output_dir}. Check path/permissions. See log.")
            
            if all_ok: self.log_to_gui("Settings applied successfully.", "info")
            else: self.log_to_gui("Some settings may not have been applied. Check logs.", "warning")

        except ValueError: messagebox.showerror("Input Error", "Please enter valid numbers for Buffer, FPS, and Quality.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to apply settings: {e}")
            self.log_to_gui(f"Failed to apply settings: {e}", "error")

    def _on_codec_change(self, event=None):
        if not self.manager: return
        selected_codec = self.codec_var.get()
        self.manager.set_codec(selected_codec)

    def _toggle_recording(self):
        if not self.manager:
            self.log_to_gui("Manager not initialized! Cannot start/stop.", "error")
            messagebox.showerror("Error", "Recorder components not initialized. Cannot start recording.")
            return

        if self.manager.running:
            self.manager.stop()
            self._stop_status_updates()
        else:
            current_output_dir = self.output_dir_var.get()
            if not current_output_dir:
                messagebox.showerror("Path Error", "Output directory is not set. Please specify in settings.")
                return

            current_buffer = self.buffer_seconds_var.get()
            current_fps = self.fps_var.get()
            current_quality = self.quality_var.get()
            current_monitor_key = self.monitor_var.get()
            current_monitor_idx = self.monitor_options.get(current_monitor_key, 1)

            needs_reinit = ( # If important settings changed, we might need to restart the recorder part.
                not self.manager.recorder or
                self.manager.recorder.output_dir != current_output_dir or
                self.manager.recorder.buffer_seconds != current_buffer or
                self.manager.recorder.fps != current_fps or
                self.manager.recorder.quality != current_quality or
                self.manager.recorder.monitor_num != current_monitor_idx 
            )

            if needs_reinit:
                self.log_to_gui("Re-initializing recorder with current GUI settings.", "info")
                try:
                    self.manager = ReplayManager( 
                        buffer_seconds=current_buffer, fps=current_fps,
                        output_dir=current_output_dir, quality=current_quality,
                        gui_status_callback=self.log_to_gui,
                        monitor_num=current_monitor_idx 
                    )
                    self.manager.set_codec(self.codec_var.get()) 
                except Exception as e:
                    self.log_to_gui(f"Error re-initializing manager with new settings: {e}", "error")
                    messagebox.showerror("Settings Error", f"Could not apply settings: {e}")
                    return
            self.manager.start() 
            if self.manager.running: 
                self._start_status_updates()
        self._update_ui_state()


    def _save_replay(self, duration):
        if not self.manager:
            messagebox.showerror("Error", "Recorder components not initialized.")
            return
        if self.manager.running:
            self.manager.trigger_replay(duration)
        else:
            messagebox.showwarning("Not Recording", "Cannot save replay. Recording is not active.")
            self.log_to_gui("Cannot save replay, recording not active.", "warning")

    def _update_ui_state(self):
        is_manager_ok = bool(self.manager) # Just checking if our main recording brain is okay.
        is_running = is_manager_ok and self.manager.running
        
        self.start_stop_btn.config(text="Stop Recording" if is_running else "Start Recording")
        if not is_manager_ok: 
            self.start_stop_btn.config(state="disabled")
        else:
            self.start_stop_btn.config(state="normal")

        settings_widget_state = "disabled" if is_running else "normal"
        if not is_manager_ok: settings_widget_state = "disabled" 
        
        self.output_entry.config(state=settings_widget_state)
        self.browse_btn.config(state=settings_widget_state)
        self.buffer_entry.config(state=settings_widget_state)
        self.fps_entry.config(state=settings_widget_state)
        self.quality_entry.config(state=settings_widget_state)
        self.apply_settings_btn.config(state=settings_widget_state)
        self.codec_combobox.config(state=settings_widget_state if is_manager_ok else "disabled")
        self.monitor_combobox.config(state=settings_widget_state if is_manager_ok else "disabled") 


        replay_btn_state = "normal" if is_running else "disabled"
        if not is_manager_ok: replay_btn_state = "disabled"
        self.save_30s_btn.config(state=replay_btn_state)
        self.save_60s_btn.config(state=replay_btn_state)
        self.save_120s_btn.config(state=replay_btn_state)

    def _start_status_updates(self):
        if not self.manager: return
        if self.status_update_job: self.master.after_cancel(self.status_update_job)
        self._update_status_labels()

    def _stop_status_updates(self):
        if self.status_update_job: self.master.after_cancel(self.status_update_job); self.status_update_job = None
        for var_name in self.status_vars:
            current_val = self.status_vars[var_name].get()
            label_part = current_val.split(':')[0]
            if "frames" in var_name: self.status_vars[var_name].set(f"{label_part}: --/-- frames")
            elif "Dropped" in var_name: self.status_vars[var_name].set(f"{label_part}: -- (0.0%)")
            else: self.status_vars[var_name].set(f"{label_part}: --")

    def _update_status_labels(self):
        if self.manager and self.manager.running: 
            try:
                resources = self.manager.get_system_resources()
                if resources:
                    self.status_vars["cpu"].set(f"CPU: {resources.get('cpu_percent', 0):.1f}%")
                    self.status_vars["mem_rss"].set(f"Mem RSS: {resources.get('memory_mb', 0):.1f} MB")
                    self.status_vars["buffer_mem"].set(f"Buffer Mem: {resources.get('buffer_memory_mb', 0):.1f} MB")
                    self.status_vars["buffer_frames"].set(f"Buffer: {resources.get('buffer_frames',0)}/{resources.get('max_buffer_frames',0)} frames")
                    self.status_vars["dropped"].set(f"Dropped: {resources.get('frames_dropped',0)} ({resources.get('drop_rate', 0):.1f}%)")
            except Exception as e: self.log_to_gui(f"Error updating status: {e}", "error")
            self.status_update_job = self.master.after(1000, self._update_status_labels)
        else: self._stop_status_updates()

    def _on_closing(self):
        if self.manager and self.manager.running:
            if messagebox.askyesno("Confirm Exit", "Recording is in progress. Are you sure you want to exit?"):
                self.manager.stop()
                self._stop_status_updates()
            else: return 
        self.master.destroy()

def cli_main(): 
    print("CLI mode is basic and does not use the GUI's advanced settings handling or logging.")
    import argparse
    parser = argparse.ArgumentParser(description='Continuous screen recorder with instant replay feature.')
    parser.add_argument('--buffer', type=int, default=120, help='Buffer duration in seconds (default: 120)')
    parser.add_argument('--fps', type=int, default=15, help='Frames per second (default: 15)')
    parser.add_argument('--quality', type=int, default=95, choices=range(0, 101), metavar="[0-100]",
                        help='Video quality (0-100, default: 95, affects ffmpeg CRF for some codecs)')
    parser.add_argument('--output', type=str, default=None, 
                        help='Output directory for video files (default: ./replays_cli in current dir)')
    parser.add_argument('--monitor', type=int, default=1, help='Monitor number to capture (default: 1, 0 is all screens)') 
    args = parser.parse_args()
    
    output_dir_path = args.output or os.path.join(os.getcwd(), "replays_cli")
    try:
        os.makedirs(output_dir_path, exist_ok=True)
    except Exception as e:
        print(f"CLI Error: Could not create output directory {output_dir_path}: {e}")
        return 

    manager = ReplayManager(
        buffer_seconds=args.buffer,
        fps=args.fps,
        output_dir=output_dir_path, 
        quality=args.quality,
        gui_status_callback=lambda msg, lvl: print(f"[CLI_RECORDER_LOG][{lvl.upper()}] {msg}"),
        monitor_num=args.monitor 
    )
    
    try:
        print("Starting recording without hotkeys for CLI demonstration...")
        manager.start() 

        print(f"\nScreen recording started. Buffering last {args.buffer}s.")
        print("Output directory:", output_dir_path)
        print("Target FPS:", args.fps)
        print("Quality:", args.quality)
        print(f"Capturing Monitor: {args.monitor}")
        print("\nPress Ctrl+C in this terminal to quit.")
        
        update_interval = 2.0
        next_update_time = time.time() + update_interval
        
        while manager.running:
            current_time = time.time()
            if current_time >= next_update_time:
                resources = manager.get_system_resources()
                if resources:
                    print("\r" + " " * 100, end="\r")
                    status_line = (
                        f"CPU: {resources['cpu_percent']:.1f}% | "
                        f"Mem: {resources['memory_mb']:.1f}MB | "
                        f"Buffer: {resources['buffer_memory_mb']:.1f}MB "
                        f"({resources['buffer_frames']}/{manager.recorder.buffer_seconds * manager.recorder.fps} frames) | "
                        f"Dropped: {resources['drop_rate']:.1f}%"
                    )
                    print(status_line, end="\r", flush=True)
                next_update_time = current_time + update_interval
            time.sleep(0.1)
    
    except KeyboardInterrupt:
        print("\nUser interruption detected (Ctrl+C).")
    
    finally:
        print("\nStopping replay manager...")
        if manager and manager.running: 
            manager.stop()
        print("Program stopped.")

if __name__ == "__main__": # This is where the magic starts for the GUI!
    root = tk.Tk()
    try:
        gui = RecorderGUI(root)
        root.mainloop()
    except Exception as main_gui_error:
        print(f"FATAL ERROR INITIALIZING GUI: {main_gui_error}")
        import traceback
        traceback.print_exc()
        if 'root' in locals() and isinstance(root, tk.Tk) and root.winfo_exists(): 
            messagebox.showerror("Application Startup Error", 
                                 f"A critical error occurred during application startup:\n\n{main_gui_error}\n\n"
                                 "The application might not work correctly or will close. "
                                 "Please check the console for more details.")
        else: 
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, f"A critical error occurred during application startup:\n\n{main_gui_error}\n\nThe application will now close. Check console output.", "Application Startup Error", 0x10) 
            except Exception:
                pass