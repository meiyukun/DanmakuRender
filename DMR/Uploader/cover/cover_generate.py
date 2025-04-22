from PIL import Image, ImageDraw, ImageFont
from moviepy import VideoFileClip
import datetime


def extract_cover_moviepy(video_path, output_path):
    try:
        clip = VideoFileClip(video_path)
        clip.save_frame(output_path, t=10)
        clip.close()
    except Exception as e:
        print(f"提取封面帧失败: {e}")


def add_text_in_pic(image_path, output_path, title: str, desc: str, bottom_text: str):
    image = Image.open(image_path)
    draw = ImageDraw.Draw(image)
    img_width, img_height = image.size  # 获取图像分辨率

    # 动态计算字体大小（按图像高度比例）
    title_font_ratio = 0.16  # 标题字体占高度15%
    desc_font_ratio = 0.15  # 描述字体占高度12%
    time_font_ratio = 0.12  # 时间字体占高度8%

    # 动态计算垂直边距（按图像高度比例）
    top_margin_ratio = 0.10  # 顶部边距5%
    bottom_margin_ratio = 0.05  # 底部边距5%

    # 加载字体（使用比例字体大小）
    try:
        bold_font_path = "fonts/cover1.ttf"
        fixed_font = ImageFont.truetype(bold_font_path, int(img_height * title_font_ratio))
        middle_font = ImageFont.truetype(bold_font_path, int(img_height * desc_font_ratio))
        time_font = ImageFont.truetype(bold_font_path, int(img_height * time_font_ratio))
    except:
        font_path = "fonts/msyh.ttc"
        fixed_font = ImageFont.truetype(font_path, int(img_height * title_font_ratio))
        middle_font = ImageFont.truetype(font_path, int(img_height * desc_font_ratio))
        time_font = ImageFont.truetype(font_path, int(img_height * time_font_ratio))

    # 计算各文本位置（水平居中，垂直按比例）
    def calculate_text_position(text, font, y_base=None, margin_ratio=0):
        _, _, text_width, text_height = draw.textbbox((0, 0), text, font=font)
        x = (img_width - text_width) // 2
        if y_base is not None:
            y = y_base - text_height - int(img_height * margin_ratio)
        else:
            y = int(img_height * margin_ratio)  # 顶部边距
        return x, y, text_height

    # 顶部标题位置（顶部边距5%）
    x_fixed, y_fixed, fixed_height = calculate_text_position(title, fixed_font, margin_ratio=top_margin_ratio)

    # 底部时间位置（底部边距5%）
    x_time, y_time, time_height = calculate_text_position(bottom_text, time_font,
                                                          y_base=img_height,
                                                          margin_ratio=bottom_margin_ratio)

    # 中间描述位置（在标题和时间之间自动居中）
    middle_y_base = y_fixed + fixed_height + int(img_height * 0.03)  # 标题与描述间距3%
    x_middle, y_middle, middle_height = calculate_text_position(desc, middle_font, y_base=y_time)
    y_middle = (y_fixed + fixed_height + y_time - time_height) // 1.6  # 垂直居中

    # 边框宽度按字体大小比例（保持视觉一致）
    border_width = max(2, int(fixed_font.size * 0.02))  # 最小边框宽度2像素

    # 绘制顶部标题（红底白边）
    draw_text_with_border(
        draw, fixed_font, x_fixed, y_fixed, title,
        text_color=(255, 0, 0), border_color=(255, 255, 255), border_width=border_width
    )

    # 绘制中间描述（橙底白边）
    draw_text_with_border(
        draw, middle_font, x_middle, y_middle, desc,
        text_color=(255, 100, 0), border_color=(255, 255, 255), border_width=border_width
    )

    # 绘制底部时间（黄底黑边）
    draw_text_with_border(
        draw, time_font, x_time, y_time, bottom_text,
        text_color=(255, 255, 0), border_color=(0, 0, 0), border_width=border_width
    )

    image.save(output_path)


def draw_text_with_border(draw, font, x, y, text, text_color, border_color, border_width):
    """带边框文本绘制（支持动态边框宽度）"""
    for dx in range(-border_width, border_width + 1):
        for dy in range(-border_width, border_width + 1):
            draw.text((x + dx, y + dy), text, font=font, fill=border_color)
    draw.text((x, y), text, font=font, fill=text_color)


def create_cover(video_path, output_pic_path, title: str, desc: str, bottom_text: str):
    extract_cover_moviepy(video_path, output_pic_path)
    add_text_in_pic(output_pic_path, output_pic_path, title, desc, bottom_text)


if __name__ == "__main__":
    video_path = r"C:\Users\53459\Videos\直播录制\羊羊不吃草（第五人格）-2025年03月06日12点01分.flv"
    output_path = r".temp/output.jpg"

    create_cover(video_path, output_path,
                 title="S1前锋单排录像",
                 desc="弹幕版！！！",
                 bottom_text=datetime.datetime.now().strftime("%Y年%m月%d日 %H:%M"))