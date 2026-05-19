import zmq
import cv2
import numpy as np
import argparse

def main():
    parser = argparse.ArgumentParser(description="ZMQ Inference test script")
    parser.add_argument("--port", type=int, default=5555, help="ZMQ port to connect to (default: 5555)")
    parser.add_argument("--cam", type=str, default="", help="Camera ID to subscribe to (e.g. 'cam_0'). Empty to get all cameras.")
    args = parser.parse_args()

    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    
    # Connect to the local port
    port = args.port
    sock.connect(f"tcp://127.0.0.1:{port}")
    
    topic = f"cam_{args.cam}" if args.cam else ""
    sock.setsockopt_string(zmq.SUBSCRIBE, topic)
    print(f"Connected to tcp://127.0.0.1:{port}, waiting for frames on topic '{topic if topic else 'ALL'}'...")

    count = 0
    out = None
    
    try:
        while True:
            # Receive multipart message
            topic_bytes = sock.recv()
            meta_json = sock.recv_json()
            frame_data = sock.recv()
            
            fmt = meta_json.get("format", "raw")
            if fmt in ("jpeg", "nvjpeg"):
                # Decode jpeg
                np_arr = np.frombuffer(frame_data, np.uint8)
                img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            else:
                # Raw array
                shape = meta_json["shape"]
                dtype = np.dtype(meta_json["dtype"])
                img = np.frombuffer(frame_data, dtype=dtype).reshape(shape)

            if img is None:
                continue



#           img - содержит кадр с 1 камеры
            # Initialize VideoWriter on the first frame
            if out is None:
                h, w = img.shape[:2]
                # Try XVID or mp4v
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                out_filename = f"capture_{meta_json.get('camera_id', 'cam')}.mp4"
                # Assuming ~20 FPS. Adjust if your camera sets a different FPS
                out = cv2.VideoWriter(out_filename, fourcc, 20.0, (w, h))
                print(f"Started recording video to {out_filename} ({w}x{h})...")

            out.write(img)
            count += 1
            if count % 30 == 0:
                print(f"[{meta_json.get('camera_id', 'cam')}] Recorded {count} frames. Format: {fmt}, Shape: {img.shape}")

    except KeyboardInterrupt:
        print("\nRecording stopped by user.")
    except Exception as e:
        print(f"Error receiving frame: {e}")
    finally:
        if out is not None:
            out.release()
            print("VideoWriter released and file saved.")

if __name__ == '__main__':
    main()
