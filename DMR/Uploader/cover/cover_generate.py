from PIL import Image, ImageDraw, ImageFont
from moviepy import VideoFileClip
import datetime


def extract_cover_moviepy(video_path, output_path):
    try:
        clip = VideoFileClip(video_path)

        clip.save_frame(output_path, t=clip.duration / 2)
        clip.close()
    except Exception as e:
        print(f"提取封面帧失败: {e}")


def get_adaptive_font(draw, text, img_width, max_font_size, min_font_size):
    font_size = max_font_size
    while True:
        try:
            font = ImageFont.truetype("fonts/cover1.ttf", font_size)
        except:
            font = ImageFont.truetype("fonts/msyh.ttc", font_size)
        _, _, text_width, _ = draw.textbbox((0, 0), text, font=font)
        if text_width <= img_width or font_size <= min_font_size:
            break
        font_size -= 1
    return font


def add_text_in_pic(image_path, output_path, title: str, desc: str, bottom_text: str):
    image = Image.open(image_path)
    draw = ImageDraw.Draw(image)
    img_width, img_height = image.size  # 获取图像分辨率

    # 动态计算垂直边距（按图像高度比例）
    top_margin_ratio = 0.14  # 顶部边距5%
    bottom_margin_ratio = 0.17  # 底部边距5%

    # 最小左右边距比例
    min_side_margin_ratio = 0.12
    min_side_margin = int(img_width * min_side_margin_ratio)

    # 自适应各文本字体大小
    title_font = get_adaptive_font(draw, title, img_width - 2 * min_side_margin, int(img_height * 0.17),
                                   int(img_height * 0.17) / 1.4)
    desc_font = get_adaptive_font(draw, desc, img_width - 2 * min_side_margin, int(img_height * 0.13),
                                  int(img_height * 0.13) / 1.5)
    bottom_font = get_adaptive_font(draw, bottom_text, img_width - 2 * min_side_margin, int(img_height * 0.12),
                                    int(img_height * 0.12) / 1.5)

    # 计算各文本位置（水平居中，垂直按比例）
    def calculate_text_position(text, font, y_base=None, margin_ratio=0):
        _, _, text_width, text_height = draw.textbbox((0, 0), text, font=font)
        x = min_side_margin + (img_width - 2 * min_side_margin - text_width) // 2
        if y_base is not None:
            y = y_base - text_height - int(img_height * margin_ratio)
        else:
            y = int(img_height * margin_ratio)  # 顶部边距
        return x, y, text_height

    # 顶部标题位置（顶部边距5%）
    x_fixed, y_fixed, fixed_height = calculate_text_position(title, title_font, margin_ratio=top_margin_ratio)

    # 底部时间位置（底部边距5%）
    x_time, y_time, time_height = calculate_text_position(bottom_text, bottom_font,
                                                          y_base=img_height,
                                                          margin_ratio=bottom_margin_ratio)

    # 中间描述位置（在标题和时间之间自动居中）
    middle_y_base = y_fixed + fixed_height + int(img_height * 0.03)  # 标题与描述间距3%
    x_middle, y_middle, middle_height = calculate_text_position(desc, desc_font, y_base=y_time)
    y_middle = (y_fixed + fixed_height + y_time - time_height) // 1.6  # 调整中间相对时间的垂直位置

    # 边框宽度按字体大小比例（保持视觉一致）
    border_width_title = max(2, int(title_font.size * 0.02))  # 最小边框宽度2像素
    border_width_desc = max(2, int(desc_font.size * 0.02))
    border_width_bottom = max(2, int(bottom_font.size * 0.02))

    # 绘制顶部标题（红底白边）
    draw_text_with_border(
        draw, title_font, x_fixed, y_fixed, title,
        text_color=(255, 0, 0), border_color=(255, 255, 255), border_width=border_width_title
    )

    # 绘制中间描述（橙底白边）
    draw_text_with_border(
        draw, desc_font, x_middle, y_middle, desc,
        text_color=(255, 100, 0), border_color=(255, 255, 255), border_width=border_width_desc
    )

    # 绘制底部时间（黄底黑边）
    draw_text_with_border(
        draw, bottom_font, x_time, y_time, bottom_text,
        text_color=(255, 255, 0), border_color=(0, 0, 0), border_width=border_width_bottom
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
                 title="S1前锋单排直播回放dddddddddddddddddddddd",
                 desc="弹幕版！！！",
                 bottom_text=datetime.datetime.now().strftime("%Y年%m月%d日 %H:%M"))
