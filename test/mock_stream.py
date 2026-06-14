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

def run_tcp_server(port=8080, fps=30):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Allow address reuse
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', port))
    server.listen(1)
    print(f"TCP 模拟服务器已启动，监听端口: {port}")
    print("支持 JustFloat 协议 (尾帧 00 00 80 7F)")
    
    interval = 1.0 / fps

    while True:
        print("等待客户端连接...")
        conn, addr = server.accept()
        print(f"客户端已连接: {addr}")
        t = 0.0
        try:
            while True:
                # 模拟 3 个通道的数据
                # 通道 1: 正弦波 (幅值10，周期约6秒)
                v1 = math.sin(t) * 10
                # 通道 2: 余弦波 (幅值5，频率较高)
                v2 = math.cos(t * 3.0) * 5
                # 通道 3: 锯齿波
                v3 = (t % 5.0) * 2 - 5
                
                packet = justfloat_pack(v1, v2, v3)
                conn.sendall(packet)
                
                t += interval
                time.sleep(interval)
        except (ConnectionAbortedError, ConnectionResetError):
            print("客户端已断开")
        except Exception as e:
            print(f"发生错误: {e}")
        finally:
            conn.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="JustFloat 模拟数据服务器")
    parser.add_argument("--port", type=int, default=8080, help="监听的 TCP 端口 (默认 8080)")
    parser.add_argument("--fps", type=int, default=30, help="发送帧率 (默认 30 Hz)")
    args = parser.parse_args()
    
    run_tcp_server(args.port, args.fps)
