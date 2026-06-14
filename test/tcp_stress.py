import socket
import struct
import time
import math
import argparse

def justfloat_pack(*floats):
    data = b''
    for f in floats:
        data += struct.pack('<f', f)
    # JustFloat Tail: 0x00, 0x00, 0x80, 0x7F
    data += bytes([0x00, 0x00, 0x80, 0x7F])
    return data

def run_stress_test(port=8080, batch_size=1000, hz=10000):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Increase send buffer size for high throughput
    server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024 * 8)
    server.bind(('0.0.0.0', port))
    server.listen(1)
    
    print(f"🚀 TCP 暴力极限压力测试已启动！")
    print(f"📡 端口: {port}")
    print(f"⚡ 目标发送速率: {hz} Hz (每秒 {hz} 个数据包)")
    print(f"📦 批处理大小: 每批发送 {batch_size} 个数据包以突破 Windows sleep 限制")
    print("-" * 50)
    
    # 预先生成大规模的动态数据缓冲
    import random
    buffer_frames = max(batch_size * 10, 100000)
    
    # 计算缓冲区的物理时间长度 T
    T = buffer_frames / hz
    # 基础频率（保证在这个频率的整数倍下，正弦波能完美首尾相接）
    f0 = 1.0 / T
    
    precomputed_packets = []
    print(f"⏳ 正在预先生成 {buffer_frames} 帧完美首尾相接的动态模拟数据...")
    
    for i in range(buffer_frames):
        t = i * (1.0 / hz)
        
        # CH1: Pitch 俯仰角 - 使用 1x 和 3x 基础频率的正弦波，保证完美循环
        v1 = math.sin(2 * math.pi * (1 * f0) * t) * 15 + math.sin(2 * math.pi * (3 * f0) * t) * 5 + random.uniform(-0.1, 0.1)
        
        # CH2: Roll 横滚角 - 使用 2x 和 4x 基础频率
        v2 = math.cos(2 * math.pi * (2 * f0) * t) * 20 + math.sin(2 * math.pi * (4 * f0) * t) * 10 + random.uniform(-0.1, 0.1)
        
        # CH3: Z轴加速度 - 基础重力 + 噪声
        v3 = 9.81 + random.uniform(-1.5, 1.5)
        # 每隔 1/5 个缓冲区时间来一次冲击跳变，保证跳变点也能完美循环
        spike_interval = max(1, int(buffer_frames * 0.2))
        if i % spike_interval < (hz * 0.005):  # 持续 5 毫秒的脉冲
            v3 += random.uniform(10, 20)
            
        # CH4: 电机 PWM 控制信号
        pwm_interval = max(1, int(buffer_frames * 0.05))
        v4 = 100.0 if (i % pwm_interval) < (pwm_interval // 2) else 0.0
        
        # CH5: 芯片/电机温度 - 用一个极慢的低频正弦波模拟温度升降，代替无法闭环的指数曲线
        v5 = 55.0 + math.sin(2 * math.pi * (1 * f0) * t - math.pi/2) * 20.0 + random.uniform(-0.02, 0.02)

        precomputed_packets.append(justfloat_pack(v1, v2, v3, v4, v5))
    
    # 将整个 10 秒的数据合并为一个巨大无比的 bytes，然后切片成批次
    full_data = b''.join(precomputed_packets)
    packet_size = len(precomputed_packets[0])
    batch_bytes = batch_size * packet_size
    print("✅ 数据包生成完毕，等待客户端连接...")

    while True:
        conn, addr = server.accept()
        print(f"🔥 客户端 {addr} 已连接，开始全速轰炸！")
        
        # 禁用 Nagle 算法，降低延迟
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        start_time = time.time()
        packets_sent = 0
        bytes_sent = 0
        
        batch_duration = batch_size / hz
        offset = 0
        total_bytes = len(full_data)
        
        try:
            while True:
                loop_start = time.time()
                
                # 从巨大的 buffer 中切片发送
                end_offset = offset + batch_bytes
                if end_offset <= total_bytes:
                    chunk = full_data[offset:end_offset]
                    offset = end_offset
                else:
                    # 缓冲区回卷
                    chunk = full_data[offset:] + full_data[:(end_offset - total_bytes)]
                    offset = end_offset - total_bytes
                    
                conn.sendall(chunk)
                
                packets_sent += batch_size
                bytes_sent += batch_bytes
                
                # 计算经过的时间并打印统计信息（每秒打印一次）
                now = time.time()
                elapsed = now - start_time
                if elapsed >= 1.0:
                    mbps = (bytes_sent * 8) / (1024 * 1024) / elapsed
                    pps = packets_sent / elapsed
                    print(f"⚡ 实时速度: {pps:.0f} Hz (数据包/秒) | 吞吐量: {mbps:.2f} Mbps")
                    start_time = now
                    packets_sent = 0
                    bytes_sent = 0
                
                # 补偿休眠时间以匹配目标 Hz
                work_time = time.time() - loop_start
                sleep_time = batch_duration - work_time
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            print("🛑 客户端已断开")
        except Exception as e:
            print(f"❌ 发生错误: {e}")
        finally:
            conn.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="JustFloat TCP 极限暴力测试")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--batch", type=int, default=1000, help="每批发送的数据包数量（过低会导致达不到高频）")
    parser.add_argument("--hz", type=int, default=10000, help="目标数据更新频率 (Hz)")
    args = parser.parse_args()
    
    run_stress_test(args.port, args.batch, args.hz)
