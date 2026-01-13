# 🎵 SpyServer Web Controller - 快速安装 / Quick Installation

<img width="2468" height="1428" alt="image" src="https://github.com/user-attachments/assets/3a80d12b-3dcf-4a01-9e72-0924d0c87187" />


## 📦 安装依赖 / Install Dependencies

```bash
pip install flask flask-socketio numpy scipy
```
```txt
Flask==2.3.0
flask-socketio==5.3.0
numpy==1.24.0
scipy==1.10.0
```

## 🚀 启动步骤 / Startup Steps

### 1. 启动 SpyServer / Start SpyServer
```bash
./spyserver  # 或 spyserver.exe (Windows) / or spyserver.exe (Windows)
```

### 2. 运行 Python 应用 / Run Python Application
```bash
python app.py
```

### 3. 打开浏览器 / Open Browser
访问 / Visit: **http://localhost:5000**

### 4. 连接并播放 / Connect and Play
1. 输入 SpyServer 地址（默认 localhost:5555）/ Enter SpyServer address (default: localhost:5555)
2. 点击"连接" / Click "Connect"
3. 调整频率（例如 FM 广播: 88-108 MHz）/ Adjust frequency (e.g., FM radio: 88-108 MHz)
4. 选择解调模式（FM）/ Select demodulation mode (FM)
5. 点击"🎵 开始接收" - 你应该会听到声音！/ Click "🎵 Start Receiving" - You should hear sound!

## 🔊 音频说明 / Audio Explanation

### 工作原理 / Working Principle
1. **接收 / Reception**: SpyServer 发送 IQ 数据 / SpyServer sends IQ data
2. **解调 / Demodulation**: Python 将 IQ 数据转换为音频 / Python converts IQ data to audio
3. **传输 / Transmission**: 通过 WebSocket 发送到浏览器 / Send to browser via WebSocket
4. **播放 / Playback**: Web Audio API 实时播放 / Real-time playback via Web Audio API

### 音频参数 / Audio Parameters
- 输出采样率 / Output Sample Rate: **48 kHz**（标准音频 / standard audio）
- 位深 / Bit Depth: **16-bit PCM**
- 通道 / Channel: **单声道 / Mono**

### 延迟 / Latency
- 网络延迟 / Network Latency: ~50-200ms
- 处理延迟 / Processing Latency: ~50-100ms
- **总延迟 / Total Latency**: 约 100-300ms / approx. 100-300ms

## ⚠️ 故障排除 / Troubleshooting

### 没有声音？/ No Sound?

1. **检查浏览器音量 / Check Browser Volume**
   - 确保浏览器标签页没有静音 / Ensure browser tab is not muted
   - 检查系统音量设置 / Check system volume settings

2. **检查频率 / Check Frequency**
   - 确保频率在信号范围内 / Ensure frequency is within signal range
   - 尝试常见的 FM 广播频率（如 107.0 MHz）/ Try common FM radio frequencies (e.g., 107.0 MHz)

3. **调整增益 / Adjust Gain**
   - 增益太低：听不到声音 / Too low gain: No sound
   - 增益太高：声音失真 / Too high gain: Distorted sound
   - 推荐从 6 开始调整 / Recommended to start adjusting from 6

4. **检查解调模式 / Check Demodulation Mode**
   - FM 广播 → 使用 FM 或 WFM / FM radio → Use FM or WFM
   - AM 广播 → 使用 AM / AM radio → Use AM
   - 不要用错模式 / Do not use the wrong mode

5. **浏览器控制台 / Browser Console**
   - 按 F12 打开开发者工具 / Press F12 to open developer tools
   - 查看 Console 标签是否有错误 / Check Console tab for errors
   - 查找 "Audio context initialized" 消息 / Look for "Audio context initialized" message

### 声音断断续续？/ Intermittent Sound?

- **网络问题 / Network Issues**: 检查 SpyServer 连接稳定性 / Check SpyServer connection stability
- **CPU 负载 / CPU Load**: 降低采样率 / Reduce sample rate
- **位深选择 / Bit Depth Selection**: 改用 Int16 而不是 Float32 / Use Int16 instead of Float32

### 声音失真？/ Distorted Sound?

- **增益太高 / Too High Gain**: 降低增益值 / Reduce gain value
- **采样率不匹配 / Mismatched Sample Rate**: 选择合适的采样率 / Select appropriate sample rate
- **解调模式错误 / Wrong Demodulation Mode**: 确认使用正确的模式 / Confirm correct mode is used

## 📊 性能优化 / Performance Optimization

### 降低延迟 / Reduce Latency
```python
# 减小音频缓冲区 / Reduce audio buffer
audio_rate=48000  # 保持标准采样率 / Keep standard sample rate
```

### 降低 CPU 使用 / Reduce CPU Usage
```python
# 选择较低的采样率 / Select lower sample rate
# 在 UI 中选择 250 kHz 而不是 10 MHz / Select 250 kHz instead of 10 MHz in UI
```

### 降低网络带宽 / Reduce Network Bandwidth
```python
# 选择 UInt8 格式 / Select UInt8 format
iq_format = 1  # 8-bit
```

## 🎯 测试建议 / Testing Recommendations

### 首次测试 - FM 广播 / First Test - FM Radio
1. 连接到 SpyServer / Connect to SpyServer
2. 频率设为: **107.0 MHz** (或你当地的 FM 电台 / or your local FM station)
3. 解调模式 / Demodulation Mode: **FM**
4. 采样率 / Sample Rate: **250 kHz** 或更高 / or higher
5. 增益 / Gain: **6**
6. 点击"开始接收" / Click "Start Receiving"
7. 应该听到清晰的 FM 广播 / You should hear clear FM radio

### 音频指示器 / Audio Indicator
- **绿色音频条 / Green Audio Bar**: 显示音频电平 / Displays audio level
- **"播放中" / "Playing"**: 有音频信号 / Audio signal present
- **"静音" / "Muted"**: 无信号或频率错误 / No signal or wrong frequency

## 🔧 进阶配置 / Advanced Configuration

### 修改音频采样率 / Modify Audio Sample Rate
在 `AudioDemodulator.__init__()` 中 / In `AudioDemodulator.__init__()`:
```python
audio_rate=48000  # 改为 44100 或其他 / Change to 44100 or other values
```

### 调整音频质量 / Adjust Audio Quality
```python
# 更高质量的低通滤波器 / Higher quality low-pass filter
cutoff = 15000  # 音频带宽 (Hz) / Audio bandwidth (Hz)
self.audio_filter = signal.butter(6, cutoff / (audio_rate / 2))
#                                  ^ 增加阶数 / Increase order
```

### 添加 AGC (自动增益控制) / Add AGC (Automatic Gain Control)
```python
def apply_agc(audio):
    target = 0.5
    current = np.max(np.abs(audio))
    if current > 0:
        audio = audio * (target / current)
    return audio
```

## 📝 日志说明 / Log Explanation

正常运行日志 / Normal running logs:
```
INFO:__main__:Connected to SpyServer at 192.168.0.110:5555
INFO:__main__:Device info received: Type=1, Serial=2365BC1B
INFO:__main__:Stream started - Mode: FM, Sample Rate: 250000 Hz
```

<img width="1263" height="496" alt="image" src="https://github.com/user-attachments/assets/b4f2aa20-5a48-4b80-9d0a-7f52980ac8ab" />


如果看到这些日志，说明已经在接收数据并解调音频！/ If you see these logs, it means data is being received and audio is being demodulated!

## 📚 相关文档 / Related Documentation

- [完整文档 / Complete Documentation](查看"安装和使用说明"artifact / Check "Installation and Usage Instructions" artifact)
- [SpyServer Protocol](https://airspy.com/spyserver/)
- [Web Audio API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API)
