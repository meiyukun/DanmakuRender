from datetime import datetime

from moviepy import VideoFileClip
import requests


def extract_audio(video_path, audio_path):
    try:
        video = VideoFileClip(video_path)
        audio = video.audio
        audio.write_audiofile(audio_path)
        print(f"音频已成功提取并保存至 {audio_path}")
    except Exception as e:
        print(f"提取音频时出现错误: {e}")
    finally:
        if 'video' in locals():
            video.close()
        if 'audio' in locals():
            audio.close()


def send_audio_to_server(audio_path, server_url):
    try:
        files = {'audio_file': open(audio_path, 'rb')}
        response = requests.post(server_url, files=files)
        response.raise_for_status()
        data = response.json()
        if 'transcription' in data:
            return data['transcription']
        elif 'error' in data:
            print(f"服务器返回错误: {data['error']}")
        else:
            print("未从服务器响应中找到 'transcription' 字段。")
    except requests.RequestException as e:
        print(f"请求服务器时出现错误: {e}")
    except ValueError:
        print("无法解析服务器响应为 JSON 格式。")
    return None


if __name__ == "__main__":
    video_path = r"C:\Users\53459\Downloads\Video\羊羊不吃草（第五人格）-2025年04月27日13点11分.mp4"
    audio_path = '.temp/output_audio_1.mp3'
    server_url = "http://localhost:8001/upload/"

    extract_audio(video_path, audio_path)
    start=datetime.now()
    transcription = send_audio_to_server(audio_path, server_url)
    end=datetime.now()
    print(f"耗时：{(end-start).total_seconds()}秒")
    if transcription:
        print(f"转录结果: {transcription}")
