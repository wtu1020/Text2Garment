import os
import rembg
from PIL import Image
import io

input_folder = "garment/normal"
output_folder = "garment/normal"

os.makedirs(output_folder, exist_ok=True)

for filename in os.listdir(input_folder):
    input_path = os.path.join(input_folder, filename)

    if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.tiff')):
        with open(input_path, "rb") as img_file:
            input_image = img_file.read()

        result = rembg.remove(input_image)

        output_path = os.path.join(output_folder, filename)
        with open(output_path, "wb") as output_file:
            output_file.write(result)
        
        print(f"处理完成: {filename} -> {output_path}")

print("所有图片处理完成！")
