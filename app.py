#!/usr/bin/env python3
"""
SpyServer Web Controller - Flask一体化版本（带音频播放）
SpyServer Web Controller - Flask Integrated Version (with Audio Playback)
使用Flask和Flask-SocketIO实现Web界面和SpyServer连接
Supports real-time audio demodulation and playback with Flask & Flask-SocketIO
支持实时音频解调和播放
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
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SpyServer Protocol Constants
SPYSERVER_PROTOCOL_VERSION = (2 << 24) | (0 << 16) | 1700
SPYSERVER_MAX_MESSAGE_BODY_SIZE = 1 << 20

# Commands
SPYSERVER_CMD_HELLO = 0
SPYSERVER_CMD_SET_SETTING = 2
SPYSERVER_CMD_PING = 3

# Settings
SPYSERVER_SETTING_STREAMING_MODE = 0
SPYSERVER_SETTING_STREAMING_ENABLED = 1
SPYSERVER_SETTING_GAIN = 2
SPYSERVER_SETTING_IQ_FORMAT = 100
SPYSERVER_SETTING_IQ_FREQUENCY = 101
SPYSERVER_SETTING_IQ_DECIMATION = 102
SPYSERVER_SETTING_IQ_DIGITAL_GAIN = 103

# Message Types
SPYSERVER_MSG_TYPE_DEVICE_INFO = 0
SPYSERVER_MSG_TYPE_UINT8_IQ = 100
SPYSERVER_MSG_TYPE_INT16_IQ = 101
SPYSERVER_MSG_TYPE_FLOAT_IQ = 103

# Device Types
SPYSERVER_DEVICE_AIRSPY_ONE = 1
SPYSERVER_DEVICE_AIRSPY_HF = 2
SPYSERVER_DEVICE_RTLSDR = 3


class AudioDemodulator:
    """音频解调器 / Audio Demodulator"""
    
    def __init__(self, mode='FM', sample_rate=250000, audio_rate=48000):
        self.mode = mode
        self.sample_rate = sample_rate
        self.audio_rate = audio_rate
        self.last_phase = 0
        
        # FM解调参数 / FM demodulation parameters
        self.fm_gain = 0.5
        
        # 设计低通滤波器（用于音频带宽限制）/ Design low-pass filter (for audio bandwidth limitation)
        cutoff = 15000  # 15kHz音频带宽 / 15kHz audio bandwidth
        self.audio_filter = signal.butter(4, cutoff / (audio_rate / 2), btype='low')
        
    def demodulate(self, iq_samples):
        """解调IQ数据为音频 / Demodulate IQ data to audio"""
        if len(iq_samples) == 0:
            return np.array([], dtype=np.float32)
        
        if self.mode == 'FM':
            return self.fm_demod(iq_samples)
        elif self.mode == 'WFM':
            return self.fm_demod(iq_samples)
        elif self.mode == 'AM':
            return self.am_demod(iq_samples)
        elif self.mode == 'USB' or self.mode == 'LSB':
            return self.ssb_demod(iq_samples)
        else:
            return self.am_demod(iq_samples)
    
    def fm_demod(self, iq_samples):
        """FM解调 / FM Demodulation"""
        # 计算相位差分 / Calculate phase difference
        phase = np.angle(iq_samples)
        
        # 相位差分 / Phase difference
        phase_diff = np.diff(np.unwrap(phase))
        audio = phase_diff * self.fm_gain
        
        # 重采样到音频采样率 / Resample to audio sample rate
        if len(audio) > 0:
            audio = self.resample(audio, self.audio_rate)
            
            # 应用低通滤波器 / Apply low-pass filter
            audio = signal.filtfilt(self.audio_filter[0], self.audio_filter[1], audio)
            
            # 归一化 / Normalization
            max_val = np.max(np.abs(audio))
            if max_val > 0:
                audio = audio / max_val * 0.8
        
        return audio.astype(np.float32)
    
    def am_demod(self, iq_samples):
        """AM解调 / AM Demodulation"""
        # 包络检波 / Envelope detection
        audio = np.abs(iq_samples)
        
        # 去除直流分量 / Remove DC component
        audio = audio - np.mean(audio)
        
        # 重采样 / Resample
        if len(audio) > 0:
            audio = self.resample(audio, self.audio_rate)
            
            # 归一化 / Normalization
            max_val = np.max(np.abs(audio))
            if max_val > 0:
                audio = audio / max_val * 0.8
        
        return audio.astype(np.float32)
    
    def ssb_demod(self, iq_samples):
        """SSB解调 / SSB Demodulation"""
        if self.mode == 'USB':
            # 上边带：取实部 / Upper Side Band: take real part
            audio = np.real(iq_samples)
        else:
            # 下边带：取虚部 / Lower Side Band: take imaginary part
            audio = np.imag(iq_samples)
        
        # 重采样 / Resample
        if len(audio) > 0:
            audio = self.resample(audio, self.audio_rate)
            
            # 归一化 / Normalization
            max_val = np.max(np.abs(audio))
            if max_val > 0:
                audio = audio / max_val * 0.8
        
        return audio.astype(np.float32)
    
    def resample(self, data, target_rate):
        """重采样到目标采样率 / Resample to target sample rate"""
        if self.sample_rate == target_rate:
            return data
        
        # 计算重采样比率 / Calculate resampling ratio
        ratio = target_rate / self.sample_rate
        num_samples = int(len(data) * ratio)
        
        if num_samples == 0:
            return np.array([], dtype=np.float32)
        
        # 使用线性插值重采样 / Resample with linear interpolation
        resampled = signal.resample(data, num_samples)
        return resampled


class SpyServerClient:
    """SpyServer协议客户端（带音频解调）/ SpyServer Protocol Client (with Audio Demodulation)"""
    
    def __init__(self, socketio):
        self.sock = None
        self.device_info = None
        self.connected = False
        self.streaming = False
        self.receive_thread = None
        self.running = False
        self.socketio = socketio
        self.demodulator = None
        self.demod_mode = 'FM'
        self.current_sample_rate = 250000
        
    def connect(self, host, port):
        """连接到SpyServer / Connect to SpyServer"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((host, port))
            self.connected = True
            self.running = True
            logger.info(f"Connected to SpyServer at {host}:{port}")
            
            # 发送握手 / Send handshake
            self.send_handshake("SpyServer Web Controller")
            
            # 启动接收线程 / Start receive thread
            self.receive_thread = threading.Thread(target=self.receive_loop, daemon=True)
            self.receive_thread.start()
            
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.connected = False
            return False
    
    def disconnect(self):
        """断开连接 / Disconnect"""
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
        """发送命令到SpyServer / Send command to SpyServer"""
        if not self.sock:
            return
        
        try:
            # 构造命令头 / Construct command header
            header = struct.pack('<II', command, len(data))
            self.sock.sendall(header + data)
        except Exception as e:
            logger.error(f"Send command error: {e}")
            self.connected = False
    
    def send_handshake(self, app_name):
        """发送握手消息 / Send handshake message"""
        app_bytes = app_name.encode('utf-8')
        data = struct.pack('<I', SPYSERVER_PROTOCOL_VERSION) + app_bytes
        self.send_command(SPYSERVER_CMD_HELLO, data)
        logger.info("Handshake sent")
    
    def set_setting(self, setting, value):
        """设置参数 / Set parameter"""
        data = struct.pack('<II', setting, value)
        self.send_command(SPYSERVER_CMD_SET_SETTING, data)
        logger.info(f"Setting {setting} = {value}")
    
    def compute_digital_gain(self, server_bits, device_gain, decimation_id):
        """计算数字增益 / Calculate digital gain"""
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
        """开始数据流 / Start data stream"""
        if not self.connected or self.streaming:
            return
        
        # 设置IQ格式 / Set IQ format
        self.set_setting(SPYSERVER_SETTING_IQ_FORMAT, settings['iq_format'])
        
        # 设置抽取率 / Set decimation
        decimation = settings['iq_decimation']
        if self.device_info:
            decimation += self.device_info['MinimumIQDecimation']
            # 计算实际采样率 / Calculate actual sample rate
            self.current_sample_rate = self.device_info['MaximumSampleRate'] // (2 ** decimation)
        
        self.set_setting(SPYSERVER_SETTING_IQ_DECIMATION, decimation)
        
        # 设置频率 / Set frequency
        self.set_setting(SPYSERVER_SETTING_IQ_FREQUENCY, settings['iq_frequency'])
        
        # 设置增益 / Set gain
        gain = settings['gain']
        self.set_setting(SPYSERVER_SETTING_GAIN, gain)
        
        # 计算并设置数字增益 / Calculate and set digital gain
        format_bits = {1: 8, 2: 16, 4: 32}
        server_bits = format_bits.get(settings['iq_format'], 32)
        digital_gain = self.compute_digital_gain(server_bits, gain, decimation)
        self.set_setting(SPYSERVER_SETTING_IQ_DIGITAL_GAIN, digital_gain)
        
        # 设置流模式为IQ Only / Set stream mode to IQ Only
        self.set_setting(SPYSERVER_SETTING_STREAMING_MODE, settings['streaming_mode'])
        
        # 初始化解调器 / Initialize demodulator
        self.demod_mode = settings.get('demod_mode', 'FM')
        self.demodulator = AudioDemodulator(
            mode=self.demod_mode,
            sample_rate=self.current_sample_rate,
            audio_rate=48000
        )
        
        # 启用流 / Enable stream
        self.set_setting(SPYSERVER_SETTING_STREAMING_ENABLED, 1)
        
        self.streaming = True
        logger.info(f"Stream started - Mode: {self.demod_mode}, Sample Rate: {self.current_sample_rate} Hz")
    
    def stop_stream(self):
        """停止数据流 / Stop data stream"""
        if not self.streaming:
            return
        
        self.set_setting(SPYSERVER_SETTING_STREAMING_ENABLED, 0)
        self.streaming = False
        self.demodulator = None
        logger.info("Stream stopped")
    
    def receive_loop(self):
        """接收消息循环 / Receive message loop"""
        try:
            while self.running and self.connected:
                # 读取消息头 (20字节) / Read message header (20 bytes)
                header_data = self.recv_exact(20)
                if not header_data:
                    break
                
                protocol_id, msg_type, stream_type, seq_num, body_size = struct.unpack('<IIIII', header_data)
                
                # 读取消息体 / Read message body
                if body_size > 0:
                    body_data = self.recv_exact(body_size)
                    if not body_data:
                        break
                    self.process_message(msg_type, body_data)
                
        except Exception as e:
            logger.error(f"Receive loop error: {e}")
        finally:
            self.connected = False
            self.socketio.emit('connection_status', {'connected': False, 'status': 'Disconnected (连接已断开)'})
    
    def recv_exact(self, size):
        """接收指定大小的数据 / Receive exact size of data"""
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
        """处理接收到的消息 / Process received message"""
        base_type = msg_type & 0xFFFF
        flags = (msg_type & 0xFFFF0000) >> 16
        
        if base_type == SPYSERVER_MSG_TYPE_DEVICE_INFO:
            # 解析设备信息 / Parse device info
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
            
            # 发送设备信息到前端 / Send device info to frontend
            self.socketio.emit('device_info', {'info': self.device_info})
            
        elif base_type in [SPYSERVER_MSG_TYPE_UINT8_IQ, SPYSERVER_MSG_TYPE_INT16_IQ, SPYSERVER_MSG_TYPE_FLOAT_IQ]:
            # IQ数据流 - 解调为音频 / IQ data stream - demodulate to audio
            if self.demodulator and self.streaming:
                try:
                    # 解析IQ数据 / Parse IQ data
                    iq_samples = self.parse_iq_data(base_type, data, flags)
                    
                    # 解调为音频 / Demodulate to audio
                    audio = self.demodulator.demodulate(iq_samples)
                    
                    if len(audio) > 0:
                        # 转换为16位PCM / Convert to 16-bit PCM
                        audio_int16 = (audio * 32767).astype(np.int16)
                        
                        # 转换为字节并发送到前端 / Convert to bytes and send to frontend
                        audio_bytes = audio_int16.tobytes()
                        audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
                        
                        self.socketio.emit('audio_data', {
                            'data': audio_b64,
                            'sample_rate': 48000
                        })
                        
                except Exception as e:
                    logger.error(f"Audio processing error: {e}")
    
    def parse_iq_data(self, msg_type, data, flags):
        """解析IQ数据 / Parse IQ data"""
        gain = pow(10, flags / 20.0)
        
        if msg_type == SPYSERVER_MSG_TYPE_UINT8_IQ:
            # UInt8格式 / UInt8 format
            samples = np.frombuffer(data, dtype=np.uint8)
            i_samples = (samples[0::2].astype(np.float32) - 128) / 128.0
            q_samples = (samples[1::2].astype(np.float32) - 128) / 128.0
            iq_samples = (i_samples + 1j * q_samples) / gain
            
        elif msg_type == SPYSERVER_MSG_TYPE_INT16_IQ:
            # Int16格式 / Int16 format
            samples = np.frombuffer(data, dtype=np.int16)
            i_samples = samples[0::2].astype(np.float32) / 32768.0
            q_samples = samples[1::2].astype(np.float32) / 32768.0
            iq_samples = (i_samples + 1j * q_samples) / gain
            
        elif msg_type == SPYSERVER_MSG_TYPE_FLOAT_IQ:
            # Float格式 / Float format
            samples = np.frombuffer(data, dtype=np.float32)
            i_samples = samples[0::2]
            q_samples = samples[1::2]
            iq_samples = (i_samples + 1j * q_samples) / gain
        else:
            iq_samples = np.array([], dtype=np.complex64)
        
        return iq_samples


# Flask应用 / Flask Application
app = Flask(__name__)
app.config['SECRET_KEY'] = 'spyserver-web-controller-secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# 全局SpyServer客户端实例 / Global SpyServer client instance
spy_client = SpyServerClient(socketio)

# HTML模板（包含音频播放功能）/ HTML Template (with audio playback)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SpyServer Web Controller (SpyServer 网页控制器)</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 50%, #1e3c72 100%);
            min-height: 100vh;
            padding: 20px;
            color: #fff;
        }
        
        .container {
            max-width: 900px;
            margin: 0 auto;
        }
        
        .header {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            padding: 25px;
            border-radius: 15px;
            margin-bottom: 20px;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .header h1 {
            font-size: 2em;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 15px;
        }
        
        .card {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            padding: 25px;
            border-radius: 15px;
            margin-bottom: 20px;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .card h2 {
            margin-bottom: 20px;
            font-size: 1.3em;
            border-bottom: 2px solid rgba(255, 255, 255, 0.3);
            padding-bottom: 10px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 500;
            color: #e0e0e0;
        }
        
        .form-row {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 15px;
        }
        
        input[type="text"],
        input[type="number"],
        select {
            width: 100%;
            padding: 12px;
            border: 1px solid rgba(255, 255, 255, 0.3);
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.3);
            color: #fff;
            font-size: 1em;
        }
        
        input[type="range"] {
            width: 100%;
            height: 8px;
            border-radius: 5px;
            background: rgba(255, 255, 255, 0.2);
            outline: none;
        }
        
        input[type="range"]::-webkit-slider-thumb {
            appearance: none;
            width: 20px;
            height: 20px;
            border-radius: 50%;
            background: #4CAF50;
            cursor: pointer;
        }
        
        .btn {
            padding: 12px 30px;
            border: none;
            border-radius: 8px;
            font-size: 1em;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            width: 100%;
        }
        
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .btn-primary {
            background: #2196F3;
            color: white;
        }
        
        .btn-primary:hover:not(:disabled) {
            background: #1976D2;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(33, 150, 243, 0.4);
        }
        
        .btn-danger {
            background: #f44336;
            color: white;
        }
        
        .btn-danger:hover:not(:disabled) {
            background: #d32f2f;
        }
        
        .btn-success {
            background: #4CAF50;
            color: white;
        }
        
        .btn-success:hover:not(:disabled) {
            background: #45a049;
        }
        
        .btn-warning {
            background: #ff9800;
            color: white;
        }
        
        .btn-warning:hover:not(:disabled) {
            background: #f57c00;
        }
        
        .status {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 15px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 8px;
            margin-top: 15px;
        }
        
        .status-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #f44336;
        }
        
        .status-dot.connected {
            background: #4CAF50;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .info-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
            font-size: 0.95em;
        }
        
        .info-item {
            padding: 10px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 8px;
        }
        
        .info-label {
            color: #b0b0b0;
            font-size: 0.85em;
        }
        
        .info-value {
            color: #fff;
            font-weight: 600;
            margin-top: 5px;
        }
        
        .freq-display {
            font-size: 1.5em;
            font-weight: 600;
            color: #4CAF50;
            text-align: center;
            padding: 15px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 8px;
            margin: 15px 0;
        }
        
        .audio-indicator {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 15px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 8px;
            margin-top: 15px;
        }
        
        .audio-bar {
            flex: 1;
            height: 8px;
            background: rgba(255, 255, 255, 0.2);
            border-radius: 4px;
            overflow: hidden;
        }
        
        .audio-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #4CAF50, #8BC34A);
            width: 0%;
            transition: width 0.1s;
        }
        
        .hidden {
            display: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📡 SpyServer Web Controller (SpyServer 网页控制器)</h1>
            <p>Real-time Audio Playback SDR Receiver Control Panel (带实时音频播放的 SDR 接收机控制面板)</p>
        </div>
        
        <!-- 连接面板 / Connection Panel -->
        <div class="card">
            <h2>🔌 Server Connection (服务器连接)</h2>
            <div class="form-row">
                <div class="form-group">
                    <label>Hostname (主机名)</label>
                    <input type="text" id="hostname" value="192.168.0.110">
                </div>
                <div class="form-group">
                    <label>Port (端口)</label>
                    <input type="number" id="port" value="5555">
                </div>
            </div>
            <button id="connectBtn" class="btn btn-primary">Connect (连接)</button>
            <div class="status">
                <div class="status-dot" id="statusDot"></div>
                <span id="statusText">Disconnected (未连接)</span>
            </div>
        </div>
        
        <!-- 参数设置面板 / Parameter Settings Panel -->
        <div class="card" id="settingsPanel" style="display: none;">
            <h2>⚙️ Parameter Settings (参数设置)</h2>
            
            <div class="form-group">
                <label>Frequency (频率)</label>
                <div class="freq-display" id="freqDisplay">107.000 MHz</div>
                <input type="range" id="freqSlider" min="24000000" max="1800000000" value="107000000" step="1000">
                <input type="number" id="freqInput" value="107000000" step="1000">
            </div>
            
            <div class="form-group">
                <label>Sample Rate (采样率)</label>
                <select id="sampleRate"></select>
            </div>
            
            <div class="form-group">
                <label>IQ Format (采样位深)</label>
                <select id="iqFormat">
                    <option value="1">UInt8 (8 bits / 8位无符号整数)</option>
                    <option value="2" selected>Int16 (16 bits / 16位有符号整数)</option>
                    <option value="4">Float32 (32 bits / 32位浮点数)</option>
                </select>
            </div>
            
            <div class="form-group">
                <label>Demodulation Mode (解调模式)</label>
                <select id="demodMode">
                    <option value="AM">AM (Amplitude Modulation / 调幅)</option>
                    <option value="FM" selected>FM (Frequency Modulation / 调频)</option>
                    <option value="WFM">WFM (Wideband FM / 宽带FM)</option>
                    <option value="USB">USB (Upper Side Band / 上边带)</option>
                    <option value="LSB">LSB (Lower Side Band / 下边带)</option>
                    <option value="CW">CW (Continuous Wave / 等幅电报)</option>
                </select>
            </div>
            
            <div class="form-group" id="gainGroup" style="display: none;">
                <label>Gain (增益): <span id="gainValue">0</span></label>
                <input type="range" id="gainSlider" min="0" max="21" value="6">
            </div>
            
            <button id="startBtn" class="btn btn-success">🎵 Start Reception (开始接收)</button>
            
            <div class="audio-indicator" id="audioIndicator" style="display: none;">
                <span>🔊 Audio (音频):</span>
                <div class="audio-bar">
                    <div class="audio-bar-fill" id="audioBarFill"></div>
                </div>
                <span id="audioStatus">Muted (静音)</span>
            </div>
        </div>
        
        <!-- 设备信息面板 / Device Information Panel -->
        <div class="card" id="devicePanel" style="display: none;">
            <h2>📊 Device Information (设备信息)</h2>
            <div class="info-grid">
                <div class="info-item">
                    <div class="info-label">Device Type (设备类型)</div>
                    <div class="info-value" id="deviceType">-</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Serial Number (序列号)</div>
                    <div class="info-value" id="deviceSerial">-</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Max Sample Rate (最大采样率)</div>
                    <div class="info-value" id="maxSampleRate">-</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Max Bandwidth (最大带宽)</div>
                    <div class="info-value" id="maxBandwidth">-</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Frequency Range (频率范围)</div>
                    <div class="info-value" id="freqRange">-</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Resolution (分辨率)</div>
                    <div class="info-value" id="resolution">-</div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        const socket = io();
        let connected = false;
        let streaming = false;
        let deviceInfo = null;
        let audioContext = null;
        let audioQueue = [];
        let isPlaying = false;
        let nextPlayTime = 0;
        
        const DEVICE_TYPES = {
            0: 'Unknown (未知设备)',
            1: 'Airspy One (Airspy One 接收机)',
            2: 'Airspy HF+ (Airspy HF+ 接收机)',
            3: 'RTL-SDR (RTL-SDR 接收机)'
        };
        
        // UI元素 / UI Elements
        const connectBtn = document.getElementById('connectBtn');
        const startBtn = document.getElementById('startBtn');
        const statusDot = document.getElementById('statusDot');
        const statusText = document.getElementById('statusText');
        const settingsPanel = document.getElementById('settingsPanel');
        const devicePanel = document.getElementById('devicePanel');
        const freqSlider = document.getElementById('freqSlider');
        const freqInput = document.getElementById('freqInput');
        const freqDisplay = document.getElementById('freqDisplay');
        const gainSlider = document.getElementById('gainSlider');
        const gainValue = document.getElementById('gainValue');
        const audioIndicator = document.getElementById('audioIndicator');
        const audioBarFill = document.getElementById('audioBarFill');
        const audioStatus = document.getElementById('audioStatus');
        
        // 初始化音频上下文 / Initialize Audio Context
        function initAudio() {
            if (!audioContext) {
                audioContext = new (window.AudioContext || window.webkitAudioContext)();
                console.log('Audio context initialized, sample rate:', audioContext.sampleRate);
            }
        }
        
        // 播放音频数据 / Play Audio Data
        function playAudio(audioData) {
            if (!audioContext || audioContext.state === 'suspended') {
                audioContext.resume();
            }
            
            try {
                // 解码Base64 / Decode Base64
                const binaryString = atob(audioData);
                const bytes = new Uint8Array(binaryString.length);
                for (let i = 0; i < binaryString.length; i++) {
                    bytes[i] = binaryString.charCodeAt(i);
                }
                
                // 转换为Int16数组 / Convert to Int16 Array
                const int16Array = new Int16Array(bytes.buffer);
                
                // 转换为Float32 / Convert to Float32
                const float32Array = new Float32Array(int16Array.length);
                for (let i = 0; i < int16Array.length; i++) {
                    float32Array[i] = int16Array[i] / 32768.0;
                }
                
                // 创建音频缓冲区 / Create Audio Buffer
                const audioBuffer = audioContext.createBuffer(1, float32Array.length, 48000);
                audioBuffer.getChannelData(0).set(float32Array);
                
                // 创建音频源 / Create Audio Source
                const source = audioContext.createBufferSource();
                source.buffer = audioBuffer;
                source.connect(audioContext.destination);
                
                // 计算播放时间 / Calculate Playback Time
                const currentTime = audioContext.currentTime;
                if (nextPlayTime < currentTime) {
                    nextPlayTime = currentTime;
                }
                
                source.start(nextPlayTime);
                nextPlayTime += audioBuffer.duration;
                
                // 更新音频指示器 / Update Audio Indicator
                const level = Math.max(...float32Array.map(Math.abs));
                audioBarFill.style.width = (level * 100) + '%';
                audioStatus.textContent = level > 0.01 ? 'Playing (播放中)' : 'Muted (静音)';
                
            } catch (error) {
                console.error('Audio playback error:', error);
            }
        }
        
        // 格式化频率 / Format Frequency
        function formatFrequency(freq) {
            if (freq >= 1e9) return (freq / 1e9).toFixed(3) + ' GHz';
            if (freq >= 1e6) return (freq / 1e6).toFixed(3) + ' MHz';
            if (freq >= 1e3) return (freq / 1e3).toFixed(3) + ' kHz';
            return freq + ' Hz';
        }
        
        // 格式化采样率 / Format Sample Rate
        function formatSampleRate(sr) {
            if (sr >= 1e6) return (sr / 1e6).toFixed(1) + ' MHz';
            if (sr >= 1e3) return (sr / 1e3).toFixed(1) + ' kHz';
            return sr + ' Hz';
        }
        
        // 更新频率显示 / Update Frequency Display
        function updateFreqDisplay() {
            const freq = parseInt(freqInput.value);
            freqDisplay.textContent = formatFrequency(freq);
            freqSlider.value = freq;
        }
        
        freqSlider.addEventListener('input', (e) => {
            freqInput.value = e.target.value;
            updateFreqDisplay();
            if (streaming) {
                socket.emit('set_frequency', { value: parseInt(e.target.value) });
            }
        });
        
        freqInput.addEventListener('input', updateFreqDisplay);
        
        gainSlider.addEventListener('input', (e) => {
            gainValue.textContent = e.target.value;
            if (streaming) {
                socket.emit('set_gain', { value: parseInt(e.target.value) });
            }
        });
        
        // 连接按钮 / Connect Button
        connectBtn.addEventListener('click', () => {
            if (!connected) {
                const hostname = document.getElementById('hostname').value;
                const port = parseInt(document.getElementById('port').value);
                socket.emit('connect_spyserver', { host: hostname, port: port });
            } else {
                socket.emit('disconnect_spyserver');
            }
        });
        
        // 开始/停止按钮 / Start/Stop Button
        startBtn.addEventListener('click', () => {
            if (!streaming) {
                // 初始化音频 / Initialize Audio
                initAudio();
                
                const settings = {
                    iq_format: parseInt(document.getElementById('iqFormat').value),
                    iq_decimation: parseInt(document.getElementById('sampleRate').value),
                    iq_frequency: parseInt(freqInput.value),
                    gain: parseInt(gainSlider.value),
                    streaming_mode: 1,
                    demod_mode: document.getElementById('demodMode').value
                };
                socket.emit('start_stream', { settings: settings });
            } else {
                socket.emit('stop_stream');
            }
        });
        
        // Socket.IO事件处理 / Socket.IO Event Handling
        socket.on('connection_status', (data) => {
            connected = data.connected;
            statusText.textContent = data.status;
            
            if (connected) {
                statusDot.classList.add('connected');
                connectBtn.textContent = 'Disconnect (断开连接)';
                connectBtn.className = 'btn btn-danger';
            } else {
                statusDot.classList.remove('connected');
                connectBtn.textContent = 'Connect (连接)';
                connectBtn.className = 'btn btn-primary';
                settingsPanel.style.display = 'none';
                devicePanel.style.display = 'none';
                streaming = false;
                startBtn.textContent = '🎵 Start Reception (开始接收)';
                startBtn.className = 'btn btn-success';
                audioIndicator.style.display = 'none';
            }
        });
        
        socket.on('device_info', (data) => {
            deviceInfo = data.info;
            settingsPanel.style.display = 'block';
            devicePanel.style.display = 'block';
            
            // 更新设备信息显示 / Update Device Info Display
            document.getElementById('deviceType').textContent = DEVICE_TYPES[deviceInfo.DeviceType] || 'Unknown (未知设备)';
            document.getElementById('deviceSerial').textContent = deviceInfo.DeviceSerial.toString(16).toUpperCase().padStart(8, '0');
            document.getElementById('maxSampleRate').textContent = formatSampleRate(deviceInfo.MaximumSampleRate);
            document.getElementById('maxBandwidth').textContent = formatSampleRate(deviceInfo.MaximumBandwidth);
            document.getElementById('freqRange').textContent = formatFrequency(deviceInfo.MinimumFrequency) + ' - ' + formatFrequency(deviceInfo.MaximumFrequency);
            document.getElementById('resolution').textContent = deviceInfo.Resolution + ' bits (位)';
            
            // 更新采样率选项 / Update Sample Rate Options
            const sampleRateSelect = document.getElementById('sampleRate');
            sampleRateSelect.innerHTML = '';
            for (let i = deviceInfo.MinimumIQDecimation; i <= deviceInfo.DecimationStageCount; i++) {
                const sr = deviceInfo.MaximumSampleRate / Math.pow(2, i);
                const option = document.createElement('option');
                option.value = i;
                option.textContent = formatSampleRate(sr);
                sampleRateSelect.appendChild(option);
            }
            
            // 更新增益控制 / Update Gain Control
            if (deviceInfo.MaximumGainIndex > 0) {
                document.getElementById('gainGroup').style.display = 'block';
                gainSlider.max = deviceInfo.MaximumGainIndex;
                gainValue.textContent = gainSlider.value;
            }
            
            // 更新频率范围 / Update Frequency Range
            freqSlider.min = deviceInfo.MinimumFrequency;
            freqSlider.max = deviceInfo.MaximumFrequency;
        });
        
        socket.on('stream_status', (data) => {
            streaming = data.streaming;
            if (streaming) {
                startBtn.textContent = '⏹ Stop Reception (停止接收)';
                startBtn.className = 'btn btn-warning';
                audioIndicator.style.display = 'flex';
                nextPlayTime = 0;
            } else {
                startBtn.textContent = '🎵 Start Reception (开始接收)';
                startBtn.className = 'btn btn-success';
                audioIndicator.style.display = 'none';
                audioBarFill.style.width = '0%';
                audioStatus.textContent = 'Muted (静音)';
            }
        });
        
        // 接收音频数据 / Receive Audio Data
        socket.on('audio_data', (data) => {
            if (streaming && audioContext) {
                playAudio(data.data);
            }
        });
        
        // 初始化 / Initialization
        updateFreqDisplay();
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    """主页 / Homepage"""
    return render_template_string(HTML_TEMPLATE)


@socketio.on('connect')
def handle_connect():
    """客户端连接 / Client Connection"""
    logger.info('Web client connected')
    emit('connection_status', {'connected': spy_client.connected, 'status': 'Connected to Web Server (已连接到Web服务器)' if spy_client.connected else 'Disconnected (未连接)'})


@socketio.on('connect_spyserver')
def handle_connect_spyserver(data):
    """连接到SpyServer / Connect to SpyServer"""
    host = data.get('host', 'localhost')
    port = data.get('port', 5555)
    
    if spy_client.connect(host, port):
        emit('connection_status', {'connected': True, 'status': 'Connecting... (正在连接...)'})
        time.sleep(1)  # 等待设备信息 / Wait for device info
        emit('connection_status', {'connected': True, 'status': 'Connected (已连接)'})
    else:
        emit('connection_status', {'connected': False, 'status': 'Connection Failed (连接失败)'})


@socketio.on('disconnect_spyserver')
def handle_disconnect_spyserver():
    """断开SpyServer连接 / Disconnect from SpyServer"""
    spy_client.disconnect()
    emit('connection_status', {'connected': False, 'status': 'Disconnected (已断开连接)'})


@socketio.on('start_stream')
def handle_start_stream(data):
    """开始数据流 / Start Data Stream"""
    settings = data.get('settings', {})
    spy_client.start_stream(settings)
    emit('stream_status', {'streaming': True})


@socketio.on('stop_stream')
def handle_stop_stream():
    """停止数据流 / Stop Data Stream"""
    spy_client.stop_stream()
    emit('stream_status', {'streaming': False})


@socketio.on('set_frequency')
def handle_set_frequency(data):
    """设置频率 / Set Frequency"""
    value = data.get('value', 100000000)
    if spy_client.connected:
        spy_client.set_setting(SPYSERVER_SETTING_IQ_FREQUENCY, value)


@socketio.on('set_gain')
def handle_set_gain(data):
    """设置增益 / Set Gain"""
    value = data.get('value', 0)
    if spy_client.connected:
        spy_client.set_setting(SPYSERVER_SETTING_GAIN, value)


if __name__ == '__main__':
    print("=" * 60)
    print("SpyServer Web Controller - With Audio Playback (SpyServer 网页控制器 - 带音频播放)")
    print("=" * 60)
    print("Starting Web Server: http://localhost:5000 (启动Web服务器: http://localhost:5000)")
    print("Please ensure SpyServer is running and listening on port 5555 (请确保SpyServer正在运行并监听端口5555)")
    print("=" * 60)
    print("Dependency Check: (依赖检查:)")
    try:
        import numpy
        print("✓ NumPy")
    except:
        print("✗ NumPy - Please run: pip install numpy (请运行: pip install numpy)")
    try:
        import scipy
        print("✓ SciPy")
    except:
        print("✗ SciPy - Please run: pip install scipy (请运行: pip install scipy)")
    print("=" * 60)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)