#!/usr/bin/env python3
"""
SpyServer Web Controller - 带瀑布图和性能优化版本
"""

import struct
import socket
import threading
import logging
import numpy as np
from flask import Flask, render_template_string
from flask_socketio import SocketIO, emit
import time
import base64
from scipy import signal
from collections import deque

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SpyServer Protocol Constants
SPYSERVER_PROTOCOL_VERSION = (2 << 24) | (0 << 16) | 1700
SPYSERVER_MAX_MESSAGE_BODY_SIZE = 1 << 20

SPYSERVER_CMD_HELLO = 0
SPYSERVER_CMD_SET_SETTING = 2

SPYSERVER_SETTING_STREAMING_MODE = 0
SPYSERVER_SETTING_STREAMING_ENABLED = 1
SPYSERVER_SETTING_GAIN = 2
SPYSERVER_SETTING_IQ_FORMAT = 100
SPYSERVER_SETTING_IQ_FREQUENCY = 101
SPYSERVER_SETTING_IQ_DECIMATION = 102
SPYSERVER_SETTING_IQ_DIGITAL_GAIN = 103

SPYSERVER_MSG_TYPE_DEVICE_INFO = 0
SPYSERVER_MSG_TYPE_UINT8_IQ = 100
SPYSERVER_MSG_TYPE_INT16_IQ = 101
SPYSERVER_MSG_TYPE_FLOAT_IQ = 103

SPYSERVER_DEVICE_AIRSPY_ONE = 1
SPYSERVER_DEVICE_AIRSPY_HF = 2
SPYSERVER_DEVICE_RTLSDR = 3


class AudioDemodulator:
    """优化的音频解调器"""
    
    def __init__(self, mode='FM', sample_rate=250000, audio_rate=48000):
        self.mode = mode
        self.sample_rate = sample_rate
        self.audio_rate = audio_rate
        self.last_phase = 0
        self.fm_gain = 0.5
        
        # 预计算重采样比率
        self.resample_ratio = audio_rate / sample_rate
        
        # 设计低通滤波器
        cutoff = 15000
        try:
            self.audio_filter = signal.butter(4, cutoff / (audio_rate / 2), btype='low')
        except:
            self.audio_filter = None
        
    def demodulate(self, iq_samples):
        """解调IQ数据为音频"""
        if len(iq_samples) == 0:
            return np.array([], dtype=np.float32)
        
        if self.mode == 'FM' or self.mode == 'WFM':
            return self.fm_demod(iq_samples)
        elif self.mode == 'AM':
            return self.am_demod(iq_samples)
        elif self.mode == 'USB' or self.mode == 'LSB':
            return self.ssb_demod(iq_samples)
        else:
            return self.am_demod(iq_samples)
    
    def fm_demod(self, iq_samples):
        """FM解调"""
        phase = np.angle(iq_samples)
        phase_diff = np.diff(np.unwrap(phase))
        audio = phase_diff * self.fm_gain
        
        if len(audio) > 0:
            audio = self.resample(audio, self.audio_rate)
            
            if self.audio_filter:
                try:
                    audio = signal.filtfilt(self.audio_filter[0], self.audio_filter[1], audio)
                except:
                    pass
            
            max_val = np.max(np.abs(audio))
            if max_val > 0:
                audio = audio / max_val * 0.8
        
        return audio.astype(np.float32)
    
    def am_demod(self, iq_samples):
        """AM解调"""
        audio = np.abs(iq_samples)
        audio = audio - np.mean(audio)
        
        if len(audio) > 0:
            audio = self.resample(audio, self.audio_rate)
            max_val = np.max(np.abs(audio))
            if max_val > 0:
                audio = audio / max_val * 0.8
        
        return audio.astype(np.float32)
    
    def ssb_demod(self, iq_samples):
        """SSB解调"""
        if self.mode == 'USB':
            audio = np.real(iq_samples)
        else:
            audio = np.imag(iq_samples)
        
        if len(audio) > 0:
            audio = self.resample(audio, self.audio_rate)
            max_val = np.max(np.abs(audio))
            if max_val > 0:
                audio = audio / max_val * 0.8
        
        return audio.astype(np.float32)
    
    def resample(self, data, target_rate):
        """优化的重采样"""
        if self.sample_rate == target_rate:
            return data
        
        num_samples = int(len(data) * self.resample_ratio)
        if num_samples == 0:
            return np.array([], dtype=np.float32)
        
        # 使用简单的线性插值，比signal.resample快
        x_old = np.linspace(0, 1, len(data))
        x_new = np.linspace(0, 1, num_samples)
        return np.interp(x_new, x_old, data)


class FFTProcessor:
    """FFT处理器用于瀑布图"""
    
    def __init__(self, fft_size=1024):
        self.fft_size = fft_size
        self.window = np.hanning(fft_size)
        
    def compute_fft(self, iq_samples):
        """计算FFT"""
        if len(iq_samples) < self.fft_size:
            return None
        
        # 取最后的fft_size个样本
        samples = iq_samples[-self.fft_size:]
        
        # 应用窗函数
        windowed = samples * self.window
        
        # 计算FFT
        fft_result = np.fft.fftshift(np.fft.fft(windowed))
        
        # 转换为dB
        magnitude = np.abs(fft_result)
        magnitude = np.maximum(magnitude, 1e-10)  # 避免log(0)
        power_db = 20 * np.log10(magnitude)
        
        # 归一化到0-255
        power_db = power_db - np.min(power_db)
        max_val = np.max(power_db)
        if max_val > 0:
            power_db = power_db / max_val * 255
        
        return power_db.astype(np.uint8)


class SpyServerClient:
    """SpyServer客户端（性能优化版本）"""
    
    def __init__(self, socketio):
        self.sock = None
        self.device_info = None
        self.connected = False
        self.streaming = False
        self.receive_thread = None
        self.running = False
        self.socketio = socketio
        self.demodulator = None
        self.fft_processor = FFTProcessor(fft_size=512)  # 较小的FFT提高性能
        self.demod_mode = 'FM'
        self.current_sample_rate = 250000
        
        # 性能优化：批处理
        self.audio_buffer = deque(maxlen=10)
        self.fft_counter = 0
        self.fft_interval = 5  # 每5个包发送一次FFT
        
        # 数据抽取用于高采样率
        self.decimate_factor = 1
        
    def connect(self, host, port):
        """连接到SpyServer"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((host, port))
            self.connected = True
            self.running = True
            logger.info(f"Connected to SpyServer at {host}:{port}")
            
            self.send_handshake("SpyServer Web Controller")
            self.receive_thread = threading.Thread(target=self.receive_loop, daemon=True)
            self.receive_thread.start()
            
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """断开连接"""
        self.running = False
        self.streaming = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        self.connected = False
        logger.info("Disconnected from SpyServer")
    
    def send_command(self, command, data):
        """发送命令到SpyServer"""
        if not self.sock:
            return
        
        try:
            header = struct.pack('<II', command, len(data))
            self.sock.sendall(header + data)
        except Exception as e:
            logger.error(f"Send command error: {e}")
            self.connected = False
    
    def send_handshake(self, app_name):
        """发送握手消息"""
        app_bytes = app_name.encode('utf-8')
        data = struct.pack('<I', SPYSERVER_PROTOCOL_VERSION) + app_bytes
        self.send_command(SPYSERVER_CMD_HELLO, data)
        logger.info("Handshake sent")
    
    def set_setting(self, setting, value):
        """设置参数"""
        data = struct.pack('<II', setting, value)
        self.send_command(SPYSERVER_CMD_SET_SETTING, data)
        logger.info(f"Setting {setting} = {value}")
    
    def compute_digital_gain(self, server_bits, device_gain, decimation_id):
        """计算数字增益"""
        if not self.device_info:
            return 0
        
        device_type = self.device_info['DeviceType']
        
        if device_type == SPYSERVER_DEVICE_AIRSPY_ONE:
            return int((self.device_info['MaximumGainIndex'] - device_gain) + (decimation_id * 3.01))
        elif device_type == SPYSERVER_DEVICE_AIRSPY_HF:
            return int(decimation_id * 3.01)
        elif device_type == SPYSERVER_DEVICE_RTLSDR:
            return int(decimation_id * 3.01)
        else:
            return 0
    
    def start_stream(self, settings):
        """开始数据流"""
        if not self.connected or self.streaming:
            return
        
        self.set_setting(SPYSERVER_SETTING_IQ_FORMAT, settings['iq_format'])
        
        decimation = settings['iq_decimation']
        if self.device_info:
            decimation += self.device_info['MinimumIQDecimation']
            self.current_sample_rate = self.device_info['MaximumSampleRate'] // (2 ** decimation)
        
        # 性能优化：高采样率时增加抽取
        if self.current_sample_rate > 2000000:
            self.decimate_factor = 4
            self.fft_interval = 10
        elif self.current_sample_rate > 1000000:
            self.decimate_factor = 2
            self.fft_interval = 8
        else:
            self.decimate_factor = 1
            self.fft_interval = 5
        
        self.set_setting(SPYSERVER_SETTING_IQ_DECIMATION, decimation)
        self.set_setting(SPYSERVER_SETTING_IQ_FREQUENCY, settings['iq_frequency'])
        
        gain = settings['gain']
        self.set_setting(SPYSERVER_SETTING_GAIN, gain)
        
        format_bits = {1: 8, 2: 16, 4: 32}
        server_bits = format_bits.get(settings['iq_format'], 32)
        digital_gain = self.compute_digital_gain(server_bits, gain, decimation)
        self.set_setting(SPYSERVER_SETTING_IQ_DIGITAL_GAIN, digital_gain)
        
        self.set_setting(SPYSERVER_SETTING_STREAMING_MODE, settings['streaming_mode'])
        
        self.demod_mode = settings.get('demod_mode', 'FM')
        self.demodulator = AudioDemodulator(
            mode=self.demod_mode,
            sample_rate=self.current_sample_rate,
            audio_rate=48000
        )
        
        self.set_setting(SPYSERVER_SETTING_STREAMING_ENABLED, 1)
        
        self.streaming = True
        self.fft_counter = 0
        logger.info(f"Stream started - Mode: {self.demod_mode}, SR: {self.current_sample_rate} Hz, Decimate: {self.decimate_factor}x")
    
    def stop_stream(self):
        """停止数据流"""
        if not self.streaming:
            return
        
        self.set_setting(SPYSERVER_SETTING_STREAMING_ENABLED, 0)
        self.streaming = False
        self.demodulator = None
        logger.info("Stream stopped")
    
    def receive_loop(self):
        """接收消息循环"""
        try:
            while self.running and self.connected:
                header_data = self.recv_exact(20)
                if not header_data:
                    break
                
                protocol_id, msg_type, stream_type, seq_num, body_size = struct.unpack('<IIIII', header_data)
                
                if body_size > 0:
                    body_data = self.recv_exact(body_size)
                    if not body_data:
                        break
                    self.process_message(msg_type, body_data)
                
        except Exception as e:
            logger.error(f"Receive loop error: {e}")
        finally:
            self.connected = False
            self.socketio.emit('connection_status', {'connected': False, 'status': '连接已断开'})
    
    def recv_exact(self, size):
        """接收指定大小的数据"""
        data = b''
        while len(data) < size:
            try:
                chunk = self.sock.recv(size - len(data))
                if not chunk:
                    return None
                data += chunk
            except socket.timeout:
                continue
            except Exception as e:
                logger.error(f"Receive error: {e}")
                return None
        return data
    
    def process_message(self, msg_type, data):
        """处理接收到的消息"""
        base_type = msg_type & 0xFFFF
        flags = (msg_type & 0xFFFF0000) >> 16
        
        if base_type == SPYSERVER_MSG_TYPE_DEVICE_INFO:
            info = struct.unpack('<IIIIIIIIIIII', data[:48])
            self.device_info = {
                'DeviceType': info[0],
                'DeviceSerial': info[1],
                'MaximumSampleRate': info[2],
                'MaximumBandwidth': info[3],
                'DecimationStageCount': info[4],
                'GainStageCount': info[5],
                'MaximumGainIndex': info[6],
                'MinimumFrequency': info[7],
                'MaximumFrequency': info[8],
                'Resolution': info[9],
                'MinimumIQDecimation': info[10],
                'ForcedIQFormat': info[11]
            }
            logger.info(f"Device info received: Type={info[0]}, Serial={info[1]:08X}")
            self.socketio.emit('device_info', {'info': self.device_info})
            
        elif base_type in [SPYSERVER_MSG_TYPE_UINT8_IQ, SPYSERVER_MSG_TYPE_INT16_IQ, SPYSERVER_MSG_TYPE_FLOAT_IQ]:
            if self.demodulator and self.streaming:
                try:
                    iq_samples = self.parse_iq_data(base_type, data, flags)
                    
                    # 抽取数据以降低处理负载
                    if self.decimate_factor > 1:
                        iq_samples = iq_samples[::self.decimate_factor]
                    
                    # 音频解调
                    audio = self.demodulator.demodulate(iq_samples)
                    
                    if len(audio) > 0:
                        audio_int16 = (audio * 32767).astype(np.int16)
                        audio_bytes = audio_int16.tobytes()
                        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
                        
                        self.socketio.emit('audio_data', {
                            'data': audio_b64,
                            'sample_rate': 48000
                        })
                    
                    # FFT计算（降低频率）
                    self.fft_counter += 1
                    if self.fft_counter >= self.fft_interval:
                        self.fft_counter = 0
                        fft_data = self.fft_processor.compute_fft(iq_samples)
                        if fft_data is not None:
                            fft_b64 = base64.b64encode(fft_data.tobytes()).decode('utf-8')
                            self.socketio.emit('fft_data', {
                                'data': fft_b64,
                                'size': len(fft_data)
                            })
                        
                except Exception as e:
                    logger.error(f"Processing error: {e}")
    
    def parse_iq_data(self, msg_type, data, flags):
        """解析IQ数据"""
        gain = pow(10, flags / 20.0)
        
        if msg_type == SPYSERVER_MSG_TYPE_UINT8_IQ:
            samples = np.frombuffer(data, dtype=np.uint8)
            i_samples = (samples[0::2].astype(np.float32) - 128) / 128.0
            q_samples = (samples[1::2].astype(np.float32) - 128) / 128.0
            iq_samples = (i_samples + 1j * q_samples) / gain
            
        elif msg_type == SPYSERVER_MSG_TYPE_INT16_IQ:
            samples = np.frombuffer(data, dtype=np.int16)
            i_samples = samples[0::2].astype(np.float32) / 32768.0
            q_samples = samples[1::2].astype(np.float32) / 32768.0
            iq_samples = (i_samples + 1j * q_samples) / gain
            
        elif msg_type == SPYSERVER_MSG_TYPE_FLOAT_IQ:
            samples = np.frombuffer(data, dtype=np.float32)
            i_samples = samples[0::2]
            q_samples = samples[1::2]
            iq_samples = (i_samples + 1j * q_samples) / gain
        else:
            iq_samples = np.array([], dtype=np.complex64)
        
        return iq_samples


# Flask应用
app = Flask(__name__)
app.config['SECRET_KEY'] = 'spyserver-web-controller-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

spy_client = SpyServerClient(socketio)

# HTML模板（完整的瀑布图UI）
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SpyServer - 瀑布图接收机</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            -webkit-tap-highlight-color: transparent;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #0a0e27;
            color: #fff;
            overflow: hidden;
            touch-action: none;
        }
        
        #connectionPanel {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 50%, #1e3c72 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }
        
        .connect-card {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(20px);
            padding: 40px;
            border-radius: 20px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            max-width: 500px;
            width: 90%;
        }
        
        .connect-card h1 {
            font-size: 2em;
            margin-bottom: 10px;
            text-align: center;
        }
        
        .connect-card p {
            text-align: center;
            opacity: 0.8;
            margin-bottom: 30px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 500;
        }
        
        .form-row {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 15px;
        }
        
        input, select {
            width: 100%;
            padding: 12px;
            border: 1px solid rgba(255, 255, 255, 0.3);
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            font-size: 1em;
        }
        
        .btn {
            width: 100%;
            padding: 15px;
            border: none;
            border-radius: 8px;
            font-size: 1.1em;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            background: #4CAF50;
            color: white;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(76, 175, 80, 0.4);
        }
        
        #mainApp {
            display: none;
            height: 100vh;
            flex-direction: column;
        }
        
        #waterfallContainer {
            flex: 1;
            position: relative;
            background: #000;
            overflow: hidden;
        }
        
        #waterfallCanvas {
            display: block;
            width: 100%;
            height: 100%;
            cursor: crosshair;
        }
        
        #tuningLine {
            position: absolute;
            left: 50%;
            top: 0;
            bottom: 0;
            width: 2px;
            background: #ff0;
            pointer-events: none;
            box-shadow: 0 0 10px #ff0;
        }
        
        #controls {
            background: rgba(10, 14, 39, 0.95);
            backdrop-filter: blur(10px);
            padding: 15px;
            border-top: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        #frequencyDisplay {
            text-align: center;
            font-size: 2em;
            font-weight: 600;
            color: #4CAF50;
            margin-bottom: 15px;
            text-shadow: 0 0 10px rgba(76, 175, 80, 0.5);
        }
        
        .control-row {
            display: flex;
            gap: 10px;
            margin-bottom: 10px;
            flex-wrap: wrap;
        }
        
        .control-group {
            flex: 1;
            min-width: 150px;
        }
        
        .control-label {
            font-size: 0.85em;
            opacity: 0.8;
            margin-bottom: 5px;
        }
        
        select.control {
            padding: 8px;
            border-radius: 5px;
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: #fff;
        }
        
        .button-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
            gap: 10px;
        }
        
        .control-btn {
            padding: 12px;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            background: rgba(255, 255, 255, 0.1);
            color: #fff;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .control-btn.active {
            background: #4CAF50;
            border-color: #4CAF50;
        }
        
        .control-btn:hover {
            background: rgba(255, 255, 255, 0.2);
        }
        
        #statusBar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 15px;
            background: rgba(0, 0, 0, 0.5);
            font-size: 0.9em;
        }
        
        .status-item {
            display: flex;
            align-items: center;
            gap: 5px;
        }
        
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #4CAF50;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        
        @media (max-width: 768px) {
            .connect-card {
                padding: 20px;
            }
            
            #frequencyDisplay {
                font-size: 1.5em;
            }
            
            .control-row {
                flex-direction: column;
            }
            
            #controls {
                padding: 10px;
            }
        }
    </style>
</head>
<body>
    <!-- 连接面板 -->
    <div id="connectionPanel">
        <div class="connect-card">
            <h1>📡 SpyServer</h1>
            <p>瀑布图接收机</p>
            
            <div class="form-row">
                <div class="form-group">
                    <label>服务器地址</label>
                    <input type="text" id="hostname" value="192.168.0.110">
                </div>
                <div class="form-group">
                    <label>端口</label>
                    <input type="number" id="port" value="5555">
                </div>
            </div>
            
            <button class="btn" onclick="connectServer()">连接</button>
            
            <div style="margin-top: 20px; text-align: center; opacity: 0.6; font-size: 0.9em;">
                <p id="connectionStatus">等待连接...</p>
            </div>
        </div>
    </div>
    
    <!-- 主应用（瀑布图） -->
    <div id="mainApp">
        <div id="waterfallContainer">
            <canvas id="waterfallCanvas"></canvas>
            <div id="tuningLine"></div>
        </div>
        
        <div id="statusBar">
            <div class="status-item">
                <div class="status-dot"></div>
                <span id="statusText">接收中</span>
            </div>
            <div class="status-item">
                <span id="bandwidthText">BW: --</span>
            </div>
        </div>
        
        <div id="controls">
            <div id="frequencyDisplay">100.000 MHz</div>
            
            <div class="control-row">
                <div class="control-group">
                    <div class="control-label">采样率</div>
                    <select id="sampleRate" class="control"></select>
                </div>
                <div class="control-group">
                    <div class="control-label">解调模式</div>
                    <select id="demodMode" class="control">
                        <option value="FM">FM</option>
                        <option value="WFM">WFM</option>
                        <option value="AM">AM</option>
                        <option value="USB">USB</option>
                        <option value="LSB">LSB</option>
                    </select>
                </div>
                <div class="control-group">
                    <div class="control-label">增益 (<span id="gainValue">6</span>)</div>
                    <input type="range" id="gainSlider" min="0" max="21" value="6" class="control" style="padding: 0;">
                </div>
            </div>
            
            <div class="button-row">
                <button class="control-btn active" id="streamBtn" onclick="toggleStream()">⏹ 停止</button>
                <button class="control-btn" onclick="adjustFreq(-1000000)">-1 MHz</button>
                <button class="control-btn" onclick="adjustFreq(-100000)">-100 kHz</button>
                <button class="control-btn" onclick="adjustFreq(100000)">+100 kHz</button>
                <button class="control-btn" onclick="adjustFreq(1000000)">+1 MHz</button>
                <button class="control-btn" onclick="showSettings()">⚙️ 设置</button>
            </div>
        </div>
    </div>
    
    <script>
        const socket = io();
        let deviceInfo = null;
        let streaming = false;
        let currentFreq = 107000000;
        let currentSampleRate = 0;
        let audioContext = null;
        let nextPlayTime = 0;
        
        // 瀑布图
        const canvas = document.getElementById('waterfallCanvas');
        const ctx = canvas.getContext('2d', { alpha: false });
        let waterfallData = [];
        const maxWaterfallLines = 400;
        let fftSize = 512;
        
        // 颜色映射（黑-蓝-青-绿-黄-红）
        const colorMap = [];
        function initColorMap() {
            for (let i = 0; i < 256; i++) {
                const t = i / 255;
                let r, g, b;
                
                if (t < 0.2) {
                    r = 0;
                    g = 0;
                    b = Math.floor(t * 5 * 255);
                } else if (t < 0.4) {
                    r = 0;
                    g = Math.floor((t - 0.2) * 5 * 255);
                    b = 255;
                } else if (t < 0.6) {
                    r = 0;
                    g = 255;
                    b = Math.floor((0.6 - t) * 5 * 255);
                } else if (t < 0.8) {
                    r = Math.floor((t - 0.6) * 5 * 255);
                    g = 255;
                    b = 0;
                } else {
                    r = 255;
                    g = Math.floor((1 - t) * 5 * 255);
                    b = 0;
                }
                
                colorMap.push({ r, g, b });
            }
        }
        initColorMap();
        
        function resizeCanvas() {
            const container = document.getElementById('waterfallContainer');
            canvas.width = container.clientWidth;
            canvas.height = container.clientHeight;
        }
        
        window.addEventListener('resize', resizeCanvas);
        resizeCanvas();
        
        function drawWaterfall() {
            if (waterfallData.length === 0) return;
            
            const width = canvas.width;
            const height = canvas.height;
            
            // 清空画布
            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, width, height);
            
            // 绘制瀑布图
            const lineHeight = Math.max(1, height / maxWaterfallLines);
            const dataWidth = waterfallData[0].length;
            const scaleX = width / dataWidth;
            
            for (let y = 0; y < waterfallData.length; y++) {
                const line = waterfallData[y];
                for (let x = 0; x < dataWidth; x++) {
                    const value = line[x];
                    const color = colorMap[value];
                    ctx.fillStyle = `rgb(${color.r},${color.g},${color.b})`;
                    ctx.fillRect(x * scaleX, y * lineHeight, Math.ceil(scaleX), Math.ceil(lineHeight));
                }
            }
            
            // 绘制频率刻度
            drawFrequencyScale();
        }
        
        function drawFrequencyScale() {
            if (!currentSampleRate) return;
            
            const width = canvas.width;
            const height = canvas.height;
            
            ctx.fillStyle = 'rgba(255, 255, 255, 0.1)';
            ctx.fillRect(0, height - 30, width, 30);
            
            ctx.font = '12px monospace';
            ctx.fillStyle = '#fff';
            ctx.textAlign = 'center';
            
            const bandwidth = currentSampleRate;
            const startFreq = currentFreq - bandwidth / 2;
            const numTicks = 5;
            
            for (let i = 0; i <= numTicks; i++) {
                const freq = startFreq + (bandwidth / numTicks) * i;
                const x = (width / numTicks) * i;
                
                ctx.fillText(formatFreq(freq), x, height - 10);
                
                ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
                ctx.beginPath();
                ctx.moveTo(x, 0);
                ctx.lineTo(x, height - 30);
                ctx.stroke();
            }
        }
        
        function formatFreq(freq) {
            if (freq >= 1e9) return (freq / 1e9).toFixed(3) + ' GHz';
            if (freq >= 1e6) return (freq / 1e6).toFixed(2) + ' MHz';
            if (freq >= 1e3) return (freq / 1e3).toFixed(1) + ' kHz';
            return freq + ' Hz';
        }
        
        function updateFrequencyDisplay() {
            document.getElementById('frequencyDisplay').textContent = formatFreq(currentFreq);
        }
        
        // 鼠标/触摸拖拽调谐
        let isDragging = false;
        let lastX = 0;
        
        canvas.addEventListener('mousedown', startDrag);
        canvas.addEventListener('touchstart', startDrag);
        canvas.addEventListener('mousemove', drag);
        canvas.addEventListener('touchmove', drag);
        canvas.addEventListener('mouseup', endDrag);
        canvas.addEventListener('touchend', endDrag);
        canvas.addEventListener('mouseleave', endDrag);
        
        function startDrag(e) {
            isDragging = true;
            lastX = e.touches ? e.touches[0].clientX : e.clientX;
            e.preventDefault();
        }
        
        function drag(e) {
            if (!isDragging || !currentSampleRate) return;
            
            const x = e.touches ? e.touches[0].clientX : e.clientX;
            const deltaX = x - lastX;
            lastX = x;
            
            // 计算频率变化
            const freqPerPixel = currentSampleRate / canvas.width;
            const deltaFreq = -deltaX * freqPerPixel;
            
            currentFreq += Math.floor(deltaFreq);
            updateFrequencyDisplay();
            
            if (streaming) {
                socket.emit('set_frequency', { value: currentFreq });
            }
            
            e.preventDefault();
        }
        
        function endDrag(e) {
            isDragging = false;
        }
        
        // 点击调谐
        canvas.addEventListener('click', (e) => {
            if (isDragging) return;
            
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const clickRatio = x / canvas.width;
            
            const bandwidth = currentSampleRate;
            const startFreq = currentFreq - bandwidth / 2;
            const clickedFreq = startFreq + (bandwidth * clickRatio);
            
            currentFreq = Math.floor(clickedFreq);
            updateFrequencyDisplay();
            
            if (streaming) {
                socket.emit('set_frequency', { value: currentFreq });
            }
        });
        
        // 音频播放
        function initAudio() {
            if (!audioContext) {
                audioContext = new (window.AudioContext || window.webkitAudioContext)();
            }
        }
        
        function playAudio(audioData) {
            if (!audioContext || audioContext.state === 'suspended') {
                audioContext.resume();
            }
            
            try {
                const binaryString = atob(audioData);
                const bytes = new Uint8Array(binaryString.length);
                for (let i = 0; i < binaryString.length; i++) {
                    bytes[i] = binaryString.charCodeAt(i);
                }
                
                const int16Array = new Int16Array(bytes.buffer);
                const float32Array = new Float32Array(int16Array.length);
                for (let i = 0; i < int16Array.length; i++) {
                    float32Array[i] = int16Array[i] / 32768.0;
                }
                
                const audioBuffer = audioContext.createBuffer(1, float32Array.length, 48000);
                audioBuffer.getChannelData(0).set(float32Array);
                
                const source = audioContext.createBufferSource();
                source.buffer = audioBuffer;
                source.connect(audioContext.destination);
                
                const currentTime = audioContext.currentTime;
                if (nextPlayTime < currentTime) {
                    nextPlayTime = currentTime;
                }
                
                source.start(nextPlayTime);
                nextPlayTime += audioBuffer.duration;
                
            } catch (error) {
                console.error('Audio error:', error);
            }
        }
        
        // 连接服务器
        function connectServer() {
            const host = document.getElementById('hostname').value;
            const port = parseInt(document.getElementById('port').value);
            
            document.getElementById('connectionStatus').textContent = '正在连接...';
            socket.emit('connect_spyserver', { host, port });
        }
        
        function adjustFreq(delta) {
            currentFreq += delta;
            updateFrequencyDisplay();
            if (streaming) {
                socket.emit('set_frequency', { value: currentFreq });
            }
        }
        
        function toggleStream() {
            const btn = document.getElementById('streamBtn');
            if (streaming) {
                socket.emit('stop_stream');
                btn.textContent = '▶️ 开始';
                btn.classList.remove('active');
            } else {
                initAudio();
                
                const settings = {
                    iq_format: 2,
                    iq_decimation: parseInt(document.getElementById('sampleRate').value),
                    iq_frequency: currentFreq,
                    gain: parseInt(document.getElementById('gainSlider').value),
                    streaming_mode: 1,
                    demod_mode: document.getElementById('demodMode').value
                };
                socket.emit('start_stream', { settings });
                btn.textContent = '⏹ 停止';
                btn.classList.add('active');
            }
        }
        
        function showSettings() {
            document.getElementById('mainApp').style.display = 'none';
            document.getElementById('connectionPanel').style.display = 'flex';
        }
        
        // Socket.IO 事件
        socket.on('connection_status', (data) => {
            document.getElementById('connectionStatus').textContent = data.status;
        });
        
        socket.on('device_info', (data) => {
            deviceInfo = data.info;
            
            // 隐藏连接面板，显示瀑布图
            document.getElementById('connectionPanel').style.display = 'none';
            document.getElementById('mainApp').style.display = 'flex';
            resizeCanvas();
            
            // 填充采样率
            const srSelect = document.getElementById('sampleRate');
            srSelect.innerHTML = '';
            for (let i = deviceInfo.MinimumIQDecimation; i <= deviceInfo.DecimationStageCount; i++) {
                const sr = deviceInfo.MaximumSampleRate / Math.pow(2, i);
                const option = document.createElement('option');
                option.value = i;
                option.textContent = formatSampleRate(sr);
                srSelect.appendChild(option);
            }
            
            // 设置增益范围
            document.getElementById('gainSlider').max = deviceInfo.MaximumGainIndex;
            
            // 更新带宽显示
            updateBandwidthDisplay();
            
            // 自动开始流
            setTimeout(() => {
                toggleStream();
            }, 500);
        });
        
        socket.on('stream_status', (data) => {
            streaming = data.streaming;
            if (!streaming) {
                waterfallData = [];
            }
        });
        
        socket.on('audio_data', (data) => {
            if (streaming && audioContext) {
                playAudio(data.data);
            }
        });
        
        socket.on('fft_data', (data) => {
            try {
                const binaryString = atob(data.data);
                const bytes = new Uint8Array(binaryString.length);
                for (let i = 0; i < binaryString.length; i++) {
                    bytes[i] = binaryString.charCodeAt(i);
                }
                
                waterfallData.unshift(Array.from(bytes));
                if (waterfallData.length > maxWaterfallLines) {
                    waterfallData.pop();
                }
                
                drawWaterfall();
            } catch (error) {
                console.error('FFT error:', error);
            }
        });
        
        function formatSampleRate(sr) {
            if (sr >= 1e6) return (sr / 1e6).toFixed(1) + ' MHz';
            if (sr >= 1e3) return (sr / 1e3).toFixed(1) + ' kHz';
            return sr + ' Hz';
        }
        
        function updateBandwidthDisplay() {
            const srSelect = document.getElementById('sampleRate');
            const decimation = parseInt(srSelect.value);
            if (deviceInfo) {
                currentSampleRate = deviceInfo.MaximumSampleRate / Math.pow(2, decimation + deviceInfo.MinimumIQDecimation);
                document.getElementById('bandwidthText').textContent = 'BW: ' + formatSampleRate(currentSampleRate);
            }
        }
        
        document.getElementById('sampleRate').addEventListener('change', updateBandwidthDisplay);
        document.getElementById('gainSlider').addEventListener('input', (e) => {
            document.getElementById('gainValue').textContent = e.target.value;
            if (streaming) {
                socket.emit('set_gain', { value: parseInt(e.target.value) });
            }
        });
        
        // 渲染循环
        function renderLoop() {
            requestAnimationFrame(renderLoop);
        }
        renderLoop();
        
        updateFrequencyDisplay();
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    """主页"""
    return render_template_string(HTML_TEMPLATE)


@socketio.on('connect')
def handle_connect():
    """客户端连接"""
    logger.info('Web client connected')
    emit('connection_status', {'connected': spy_client.connected, 'status': '已连接到Web服务器' if spy_client.connected else '未连接'})


@socketio.on('connect_spyserver')
def handle_connect_spyserver(data):
    """连接到SpyServer"""
    host = data.get('host', 'localhost')
    port = data.get('port', 5555)
    
    if spy_client.connect(host, port):
        emit('connection_status', {'connected': True, 'status': '正在连接...'})
        time.sleep(1)
        emit('connection_status', {'connected': True, 'status': '已连接'})
    else:
        emit('connection_status', {'connected': False, 'status': '连接失败'})


@socketio.on('disconnect_spyserver')
def handle_disconnect_spyserver():
    """断开SpyServer连接"""
    spy_client.disconnect()
    emit('connection_status', {'connected': False, 'status': '已断开连接'})


@socketio.on('start_stream')
def handle_start_stream(data):
    """开始数据流"""
    settings = data.get('settings', {})
    spy_client.start_stream(settings)
    emit('stream_status', {'streaming': True})


@socketio.on('stop_stream')
def handle_stop_stream():
    """停止数据流"""
    spy_client.stop_stream()
    emit('stream_status', {'streaming': False})


@socketio.on('set_frequency')
def handle_set_frequency(data):
    """设置频率"""
    value = data.get('value', 100000000)
    if spy_client.connected:
        spy_client.set_setting(SPYSERVER_SETTING_IQ_FREQUENCY, value)


@socketio.on('set_gain')
def handle_set_gain(data):
    """设置增益"""
    value = data.get('value', 0)
    if spy_client.connected:
        spy_client.set_setting(SPYSERVER_SETTING_GAIN, value)


if __name__ == '__main__':
    print("=" * 60)
    print("🌊 SpyServer Web Controller - 瀑布图接收机")
    print("=" * 60)
    print("🌐 Web界面: http://localhost:5000")
    print("📡 确保SpyServer运行在端口5555")
    print("=" * 60)
    print("✨ 功能特性:")
    print("  • 实时瀑布图显示")
    print("  • 鼠标/触摸拖拽调谐")
    print("  • 点击频谱快速跳转")
    print("  • 音频实时解调播放")
    print("  • 性能优化（支持10MHz采样率）")
    print("  • 响应式设计（支持手机/平板/电脑）")
    print("=" * 60)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
