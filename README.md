# HashDedup — 照片 / 视频去重工具

按文件**内容**（字节级哈希）找出重复的照片和视频，自动分流到两个文件夹：
- **保留文件夹**：所有唯一内容（无重复的文件 + 每组重复中选出的保留者）
- **重复文件夹**：每组中的冗余副本，另附一份 `dedup_report.md` 去重报告

两个目标文件夹均为**平铺结构**（不保留原目录层级），同名文件自动编号为
`名称 (1).ext`、`名称 (2).ext`…；如果目标里已存在**内容相同**的文件（任意名字），
会直接跳过而不重复复制。**源文件夹全程只读，不会被修改。**

## 使用方法

```bash
# 首次：安装依赖
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# 启动（会自动打开浏览器）
.venv/Scripts/python app.py
```

打开 http://127.0.0.1:8765 ，填三个路径（源 / 保留 / 重复文件夹，可点「浏览…」选择），
「开始扫描」→ 查看分组结果（绿框为自动选出的保留者，可点击换人）→「执行复制」。

## 保留者自动选择规则

依次比较：文件更大 → 修改时间更早 → 创建时间更早 → 路径更短（保证稳定可复现）。

## 支持的格式

- 图片：jpg / jpeg / png / gif / bmp / webp / tif / tiff / heic / heif / avif / ico
- 视频：mp4 / mov / avi / mkv / wmv / webm / flv / m4v / mpg / mpeg / 3gp / mts

扫描策略：先按文件大小分组（大小不同必不重复）→ 同大小比前 64KB 前缀哈希 →
前缀相同再算全文件哈希，因此大照片库也不会做无用功。视频与图片用同一套字节级判定。

## 结构

```
app.py               入口：启动服务 + 自动打开浏览器
dedup/scanner.py     递归遍历 + 两阶段哈希分组
dedup/planner.py     保留者自动选择
dedup/executor.py    平铺复制 + 重名编号 + 去重报告
dedup/thumbs.py      缩略图生成（Pillow）
dedup/server.py      FastAPI 服务与 API
static/              前端页面（原生 HTML/JS/CSS，无构建步骤）
```
