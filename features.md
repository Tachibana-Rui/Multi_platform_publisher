# 多平台媒体发布 Agent 初版系统设计

## 1. 项目目标

本系统旨在构建一个多平台媒体发布 Agent，用于将同一份原创内容适配并发布到多个内容平台，例如小红书、抖音、B 站、快手、视频号等。

初版目标聚焦于：

* 管理原创图片、视频、标题、正文、标签等内容资产。
* 将小红书风格的内容适配为抖音风格内容。
* 支持抖音平台的自动化或半自动化发布。
* 支持小红书作为内容发布端或内容参考来源。
* 提供人工审核机制，避免违规发布、误发、重复发布。
* 为后续扩展到更多平台预留统一接口。

系统设计原则是：

> 不做“小红书爬虫搬运器”，而是做“多平台内容发布中台”。

---

## 2. 核心设计思路

推荐采用“内容中台优先”的架构，而不是直接从小红书页面抓取已发布作品再搬运到抖音。

原因如下：

1. 更合规
   避免批量爬取、反爬绕过、去水印、Cookie 复用等高风险行为。

2. 更稳定
   官方 API 和自有内容库比网页爬虫、移动端自动化更稳定。

3. 更易扩展
   后续可扩展到 B 站、快手、视频号、TikTok 等平台。

4. 更利于内容复用
   原始素材可以按不同平台重新裁剪、压缩、改写、排期。

5. 更利于数据追踪
   可以统一记录每个平台的发布状态、作品 ID、失败原因、发布时间和审核状态。

---

## 3. 总体架构

```text
原创素材 / 文案 / 草稿
        ↓
Content Hub 内容中台
        ↓
内容处理与平台适配
        ↓
平台发布器
   ├── 小红书发布器
   ├── 抖音发布器
   ├── B站发布器
   ├── 快手发布器
   └── 视频号发布器
        ↓
发布状态追踪 / 数据回收 / 人工审核
```

---

## 4. 系统模块划分

### 4.1 Content Ingestion 内容导入模块

用于将内容导入系统。

支持来源：

* 手动上传图片或视频。
* 手动输入标题、正文、标签。
* 从本地文件夹导入。
* 从已有小红书链接半自动导入，仅限用户本人拥有版权或授权的内容。

主要功能：

* 上传媒体文件。
* 创建内容草稿。
* 解析标题、正文、标签。
* 识别内容类型：图文、视频、图集。
* 保存原始素材，不依赖平台水印版本。

---

### 4.2 Asset Storage 素材存储模块

用于保存图片、视频、封面图、压缩版本等文件。

可选技术：

* 本地文件系统。

存储内容包括：

* 原始图片。
* 原始视频。
* 抖音适配版本。
* 小红书适配版本。
* 封面图。
* 缩略图。
* 转码后视频。
* 平台发布用临时文件。

---

### 4.3 Metadata DB 内容数据库模块

用于保存内容元数据和平台发布状态。

核心数据包括：

* 内容标题。
* 内容正文。
* 标签。
* 媒体文件地址。
* 来源平台。
* 发布目标平台。
* 每个平台的发布状态。
* 平台作品 ID。
* 审核状态。
* 失败原因。
* 发布时间。
* 创建时间和更新时间。

---

### 4.4 Content Adapter 内容适配模块

用于将同一份内容改写为不同平台适用的版本。

例如小红书内容通常偏向：

* 种草语气。
* 图文笔记。
* 生活经验分享。
* 关键词密集。
* emoji 和标签较多。

抖音内容通常更偏向：

* 短标题。
* 强钩子。
* 视频优先。
* 简洁描述。
* 更适合短视频推荐流的开头表达。

适配模块负责：

* 小红书标题改写为抖音标题。
* 正文压缩。
* 生成抖音简介。
* 生成平台 hashtag。
* 检查是否存在过度营销、违禁词、导流词。
* 检查是否存在其他平台水印。
* 生成平台专用发布草稿。

---

### 4.5 Media Processor 媒体处理模块

用于检查和处理图片、视频格式。

核心功能：

* 检查视频格式。
* 检查视频时长。
* 检查视频大小。
* 检查视频分辨率。
* 检查图片大小。
* 检查图片比例。
* 自动生成封面。
* 使用 FFmpeg 转码。
* 根据平台规则压缩媒体。
* 检查是否存在明显平台水印。

可使用技术：

```text
FFmpeg
OpenCV
Pillowll
moviepy
MediaInfo
```

---

### 4.6 Platform Publisher 平台发布器模块

每个平台实现一个独立 Publisher。

统一接口示例：

```python
from abc import ABC, abstractmethod

class Publisher(ABC):
    @abstractmethod
    def validate(self, post):
        pass

    @abstractmethod
    def publish(self, post):
        pass

    @abstractmethod
    def get_status(self, publication_id):
        pass
```

抖音发布器示例：

```python
class DouyinPublisher(Publisher):
    def __init__(self, access_token: str):
        self.access_token = access_token

    def validate(self, post):
        # 检查视频格式、图片大小、标题长度、是否存在水印等
        return True

    def publish(self, post):
        # 1. 上传媒体文件
        # 2. 创建抖音作品
        # 3. 返回平台作品 ID
        pass

    def get_status(self, publication_id):
        # 查询发布状态或审核状态
        pass
```

小红书发布器初版可以先做半自动：

```python
class XiaohongshuPublisher(Publisher):
    def validate(self, post):
        # 检查图片数量、标题、正文、标签等
        return True

    def publish(self, post):
        # 初版可以使用人工上传或 RPA 半自动发布
        pass

    def get_status(self, publication_id):
        # 初版可人工标记状态
        pass
```

---

### 4.7 Agent Orchestrator 调度 Agent

Agent Orchestrator 是系统的任务调度核心。

职责包括：

* 接收发布任务。
* 判断目标平台。
* 调用内容适配模块。
* 调用媒体处理模块。
* 调用平台发布器。
* 处理失败重试。
* 记录发布日志。
* 将高风险内容交给人工审核。
* 控制定时发布流程。

任务流示例：

```text
读取内容草稿
    ↓
判断目标平台
    ↓
生成平台适配文案
    ↓
检查媒体格式
    ↓
生成发布预览
    ↓
人工确认
    ↓
调用发布器
    ↓
保存发布结果
```

---

### 4.8 Human Review 人工审核模块

为了降低账号风险，初版建议保留人工确认环节。

人工审核内容包括：

* 标题是否合适。
* 正文是否夸张。
* 是否存在敏感词。
* 是否存在外站导流。
* 是否存在平台水印。
* 视频封面是否合适。
* 发布时间是否正确。
* 发布平台是否正确。

发布前状态流：

```text
draft 草稿
  ↓
adapted 已适配
  ↓
validated 已检查
  ↓
review_pending 待审核
  ↓
approved 已批准
  ↓
publishing 发布中
  ↓
published 已发布
```

---

## 5. 推荐技术栈

### 5.1 后端

可选方案：

```text
Python + FastAPI
Node.js + NestJS
```

推荐初版使用：

```text
Python + FastAPI
```

原因：

* 适合做 Agent 编排。
* 方便接入 LLM。
* 方便使用 FFmpeg、OpenCV、Pillow 等媒体处理工具。
* 与 Playwright、Celery、数据库集成方便。

---

### 5.2 前端

可选方案：

```text
React
Next.js
Vue
```

推荐：

```text
Next.js + Tailwind CSS
```

核心页面：

* 内容列表页。
* 内容编辑页。
* 平台发布配置页。
* 发布预览页。
* 审核确认页。
* 发布状态页。
* 失败日志页。

---

### 5.3 数据库

推荐：

```text
PostgreSQL
```

理由：

* 适合结构化内容管理。
* 支持 JSONB 字段保存平台差异化配置。
* 方便后续扩展统计分析。

---

### 5.4 任务队列

推荐：

```text
Celery + Redis
```

或：

```text
BullMQ + Redis
```

用途：

* 异步发布任务。
* 视频转码任务。
* 定时发布任务。
* 发布失败重试。
* 状态轮询。

---

### 5.5 媒体处理

推荐：

```text
FFmpeg
OpenCV
Pillow
MediaInfo
```

用途：

* 视频转码。
* 视频压缩。
* 图片压缩。
* 封面生成。
* 分辨率检查。
* 文件格式检查。

---

### 5.6 浏览器自动化兜底

当平台没有稳定开放 API 时，可使用：

```text
Playwright
Selenium
Appium
uiautomator2
```

推荐优先级：

```text
官方 API > 半自动网页表单 > RPA 自动化 > 移动端自动化
```

不建议做：

* 绕过验证码。
* Cookie 窃取。
* 自动登录绕过。
* 代理池规避风控。
* 批量无人值守发布。
* 大规模抓取别人内容。

---

## 6. 数据库设计初版

### 6.1 posts 表

```sql
CREATE TABLE posts (
    id UUID PRIMARY KEY,
    title TEXT,
    body TEXT,
    source_platform TEXT,
    source_url TEXT,
    content_type TEXT,
    status TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

字段说明：

| 字段              | 说明                           |
| --------------- | ---------------------------- |
| id              | 内容 ID                        |
| title           | 原始标题                         |
| body            | 原始正文                         |
| source_platform | 来源平台，例如 manual、xhs、notion    |
| source_url      | 来源链接                         |
| content_type    | image、video、gallery          |
| status          | draft、ready、published、failed |
| created_at      | 创建时间                         |
| updated_at      | 更新时间                         |

---

### 6.2 media_assets 表

```sql
CREATE TABLE media_assets (
    id UUID PRIMARY KEY,
    post_id UUID REFERENCES posts(id),
    file_url TEXT,
    media_type TEXT,
    width INT,
    height INT,
    duration_seconds FLOAT,
    file_size BIGINT,
    checksum TEXT,
    created_at TIMESTAMP
);
```

字段说明：

| 字段               | 说明            |
| ---------------- | ------------- |
| id               | 素材 ID         |
| post_id          | 对应内容 ID       |
| file_url         | 文件地址          |
| media_type       | image 或 video |
| width            | 宽度            |
| height           | 高度            |
| duration_seconds | 视频时长          |
| file_size        | 文件大小          |
| checksum         | 文件校验值         |
| created_at       | 创建时间          |

---

### 6.3 platform_publications 表

```sql
CREATE TABLE platform_publications (
    id UUID PRIMARY KEY,
    post_id UUID REFERENCES posts(id),
    platform TEXT,
    platform_item_id TEXT,
    publish_status TEXT,
    error_message TEXT,
    scheduled_at TIMESTAMP,
    published_at TIMESTAMP,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

字段说明：

| 字段               | 说明                                           |
| ---------------- | -------------------------------------------- |
| id               | 发布记录 ID                                      |
| post_id          | 对应内容 ID                                      |
| platform         | douyin、xhs、bilibili 等                        |
| platform_item_id | 平台作品 ID                                      |
| publish_status   | pending、publishing、published、failed、rejected |
| error_message    | 失败原因                                         |
| scheduled_at     | 计划发布时间                                       |
| published_at     | 实际发布时间                                       |
| created_at       | 创建时间                                         |
| updated_at       | 更新时间                                         |

---

### 6.4 platform_versions 表

用于保存不同平台的文案版本。

```sql
CREATE TABLE platform_versions (
    id UUID PRIMARY KEY,
    post_id UUID REFERENCES posts(id),
    platform TEXT,
    title TEXT,
    body TEXT,
    hashtags TEXT[],
    cover_url TEXT,
    extra_config JSONB,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

字段说明：

| 字段           | 说明      |
| ------------ | ------- |
| id           | 平台版本 ID |
| post_id      | 对应内容 ID |
| platform     | 目标平台    |
| title        | 平台适配标题  |
| body         | 平台适配正文  |
| hashtags     | 平台标签    |
| cover_url    | 封面地址    |
| extra_config | 平台差异化配置 |
| created_at   | 创建时间    |
| updated_at   | 更新时间    |

---

## 7. 核心工作流

### 7.1 手动创建内容

```text
用户上传素材
    ↓
输入标题、正文、标签
    ↓
保存为草稿
    ↓
系统分析内容类型
    ↓
生成平台适配版本
    ↓
进入审核队列
```

---

### 7.2 小红书内容同步到抖音

推荐流程：

```text
用户提供小红书原始内容或链接
    ↓
系统创建内容草稿
    ↓
用户上传无水印原始素材
    ↓
Agent 提取或改写标题、正文、标签
    ↓
生成抖音版本
    ↓
检查媒体格式
    ↓
人工审核
    ↓
发布到抖音
```

注意：

> 不建议直接从小红书下载带水印内容再搬运到抖音。更推荐使用用户本地保存的原始素材。

---

### 7.3 抖音发布流程

```text
用户授权抖音账号
    ↓
系统获取 access_token
    ↓
校验媒体文件
    ↓
上传视频或图片
    ↓
创建抖音作品
    ↓
保存抖音作品 ID
    ↓
查询审核状态
    ↓
更新发布记录
```

---

### 7.4 小红书发布流程

初版可以采用半自动流程：

```text
生成小红书文案
    ↓
准备图片或视频
    ↓
打开小红书创作者中心
    ↓
用户手动登录
    ↓
系统自动填写标题、正文、标签、上传素材
    ↓
用户最终确认发布
    ↓
系统记录发布状态
```

后续如果有可用官方 API，再替换为 API 发布。

---

## 8. Agent 能力设计

### 8.1 Content Analyzer Agent

负责分析内容。

输入：

* 标题。
* 正文。
* 图片。
* 视频。
* 标签。

输出：

* 内容类型。
* 适合发布的平台。
* 是否需要改写。
* 是否存在敏感内容。
* 是否存在平台水印。
* 是否适合短视频化。

---

### 8.2 Caption Rewrite Agent

负责文案改写。

示例 Prompt：

```text
将下面的小红书笔记改写为适合抖音发布的标题和简介。

要求：
1. 标题更短，更适合短视频推荐流。
2. 保留核心卖点。
3. 不要夸大宣传。
4. 不要加入无法验证的承诺。
5. 不要添加外站导流。
6. 输出 JSON。

输入：
标题：{title}
正文：{body}
标签：{tags}

输出格式：
{
  "douyin_title": "",
  "douyin_body": "",
  "hashtags": []
}
```

---

### 8.3 Media Validation Agent

负责媒体校验。

检查项：

* 视频格式。
* 视频大小。
* 视频时长。
* 图片大小。
* 图片比例。
* 文件损坏。
* 是否有明显水印。
* 是否符合目标平台限制。

示例代码：

```python
class MediaValidator:
    def validate_for_douyin(self, media):
        if media.type == "video":
            if media.format not in ["mp4", "webm"]:
                raise ValueError("Unsupported video format")

            if media.duration_seconds > 15 * 60:
                raise ValueError("Video duration exceeds platform limit")

        if media.type == "image":
            if media.size_bytes > 100 * 1024 ** 2:
                raise ValueError("Image file is too large")

        return True
```

---

### 8.4 Publish Agent

负责执行发布任务。

主要逻辑：

```text
读取待发布任务
    ↓
检查目标平台
    ↓
调用平台发布器
    ↓
失败时记录原因
    ↓
必要时进入重试队列
    ↓
成功后保存平台作品 ID
```

---

### 8.5 Scheduler Agent

负责定时发布。

能力：

* 设置发布时间。
* 避免同一平台短时间重复发布。
* 支持每日发布计划。
* 支持失败后延迟重试。
* 支持人工审核后再进入发布队列。

---

## 9. API 设计初版

### 9.1 创建内容草稿

```http
POST /api/posts
```

请求体：

```json
{
  "title": "原始标题",
  "body": "原始正文",
  "source_platform": "manual",
  "content_type": "video",
  "tags": ["tag1", "tag2"]
}
```

---

### 9.2 上传素材

```http
POST /api/posts/{post_id}/assets
```

请求类型：

```text
multipart/form-data
```

---

### 9.3 生成平台适配版本

```http
POST /api/posts/{post_id}/adapt
```

请求体：

```json
{
  "target_platform": "douyin"
}
```

返回：

```json
{
  "title": "抖音标题",
  "body": "抖音简介",
  "hashtags": ["tag1", "tag2"]
}
```

---

### 9.4 创建发布任务

```http
POST /api/publications
```

请求体：

```json
{
  "post_id": "uuid",
  "platform": "douyin",
  "scheduled_at": "2026-06-21T20:00:00+09:00"
}
```

---

### 9.5 审核发布任务

```http
POST /api/publications/{publication_id}/approve
```

---

### 9.6 查询发布状态

```http
GET /api/publications/{publication_id}
```

返回：

```json
{
  "publication_id": "uuid",
  "platform": "douyin",
  "status": "published",
  "platform_item_id": "platform_item_id",
  "published_at": "2026-06-21T20:05:00+09:00"
}
```

---

## 10. 项目目录结构建议

```text
media-agent/
  backend/
    app.py
    config.py

    models/
      post.py
      media.py
      publication.py
      platform_version.py

    services/
      asset_store.py
      ffmpeg_service.py
      text_adapter.py
      media_validator.py
      watermark_checker.py

    agents/
      content_analyzer.py
      caption_rewriter.py
      media_validation_agent.py
      publish_agent.py
      scheduler_agent.py

    publishers/
      base.py
      douyin.py
      xiaohongshu_rpa.py
      bilibili.py

    workers/
      publish_worker.py
      transcode_worker.py
      status_poll_worker.py

    api/
      posts.py
      assets.py
      publications.py
      platforms.py

  frontend/
    pages/
      dashboard.tsx
      post_editor.tsx
      review.tsx
      publication_status.tsx

    components/
      PostCard.tsx
      MediaUploader.tsx
      PlatformPreview.tsx
      ReviewPanel.tsx

  storage/
    raw/
    processed/
    covers/

  docker-compose.yml
  README.md
```

---

## 11. MVP 开发阶段

### 阶段 1：内容库与手动导入

目标：

* 支持创建内容草稿。
* 支持上传图片和视频。
* 支持保存标题、正文、标签。
* 支持查看内容列表。

功能：

```text
创建内容
上传素材
编辑内容
查看草稿
删除草稿
```

---

### 阶段 2：抖音文案适配

目标：

* 将小红书风格文案改写为抖音风格。
* 生成抖音标题、简介和标签。
* 支持人工编辑适配结果。

功能：

```text
AI 改写标题
AI 改写正文
生成 hashtag
人工修改
保存平台版本
```

---

### 阶段 3：媒体校验与处理

目标：

* 检查视频和图片是否适合目标平台发布。
* 支持基础视频转码和图片压缩。

功能：

```text
检查视频格式
检查视频时长
检查文件大小
生成封面
压缩图片
转码视频
```

---

### 阶段 4：抖音发布

目标：

* 接入抖音开放平台。
* 支持用户授权。
* 支持上传媒体。
* 支持创建作品。
* 支持查询发布状态。

功能：

```text
OAuth 授权
上传视频
上传图片
发布作品
查询状态
保存作品 ID
```

---

### 阶段 5：小红书半自动发布

目标：

* 暂时不强依赖小红书 API。
* 使用半自动发布流程。
* 用户保留最终确认权。

功能：

```text
打开发布页面
自动填入标题
自动填入正文
自动上传素材
用户确认发布
记录状态
```

---

## 12. 风险与边界

### 12.1 合规风险

不建议实现：

* 批量抓取小红书内容。
* 抓取其他用户作品。
* 自动去水印。
* 绕过验证码。
* 使用代理池规避平台风控。
* 盗用 Cookie。
* 无人值守批量发布。
* 搬运未授权内容。

建议实现：

* 只处理用户自己拥有版权或授权的内容。
* 优先使用原始无水印素材。
* 优先使用官方 API。
* 发布前保留人工审核。
* 限制发布频率。
* 保存操作日志。

---

### 12.2 账号安全风险

需要注意：

* 高频发布可能触发风控。
* 内容重复可能影响推荐。
* 带其他平台水印可能影响审核。
* 营销导流词可能导致限流或拒审。
* RPA 自动化可能因页面改版失效。

建议：

* 控制发布频率。
* 避免完全无人值守。
* 发布前做内容检查。
* 重要账号不要直接用于测试。
* 使用测试账号验证流程。

---

### 12.3 技术风险

可能问题：

* 平台 API 权限申请失败。
* 平台规则变化。
* 视频格式不兼容。
* RPA 选择器失效。
* OAuth token 过期。
* 发布状态回调不稳定。
* 大文件上传失败。

解决策略：

* 平台发布器模块化。
* 失败重试。
* 状态轮询。
* 日志追踪。
* 手动补偿流程。
* 保留人工发布兜底方案。

---

## 13. 初版优先级

推荐优先级如下：

```text
P0：内容库
P0：素材上传
P0：抖音文案适配
P0：媒体格式校验
P1：抖音官方 API 发布
P1：人工审核
P2：小红书半自动发布
P2：定时发布
P3：数据回收
P3：多平台扩展
P4：自动选题与内容策略 Agent
```

---

## 14. 最小可行系统 MVP

MVP 可以只包含以下能力：

```text
1. 上传原始视频或图片。
2. 输入小红书标题和正文。
3. AI 自动生成抖音标题、简介和标签。
4. 检查媒体格式。
5. 人工确认发布内容。
6. 调用抖音发布能力。
7. 保存发布状态和作品 ID。
```

MVP 数据流：

```text
用户上传内容
    ↓
保存草稿
    ↓
生成抖音版本
    ↓
媒体校验
    ↓
人工确认
    ↓
发布抖音
    ↓
记录结果
```

---

## 15. 后续扩展方向

### 15.1 多平台扩展

未来可以增加：

* B 站。
* 快手。
* 视频号。
* TikTok。
* Instagram Reels。
* YouTube Shorts。

---

### 15.2 数据回收

可以统计：

* 播放量。
* 点赞数。
* 评论数。
* 收藏数。
* 转发数。
* 完播率。
* 粉丝增长。
* 发布时间效果。

---

### 15.3 内容策略 Agent

未来可以加入：

* 自动选题。
* 爆款标题分析。
* 发布时间推荐。
* 平台差异化改写。
* 内容重复度检查。
* 多平台表现对比。
* 自动生成复盘报告。

---

## 16. 总结

初版系统不应设计为“小红书内容爬取并搬运到抖音”的工具，而应设计为一个“多平台媒体发布中台”。

推荐实现路径：

```text
自有内容库
    ↓
平台文案适配
    ↓
媒体格式校验
    ↓
人工审核
    ↓
平台发布器
    ↓
状态追踪
```

其中：

* 抖音优先走官方开放能力。
* 小红书初版可以作为内容输入参考或半自动发布端。
* 所有内容应来自用户本人原创或已授权素材。
* 发布前保留人工审核。
* 系统架构应以平台适配器模式设计，便于后续扩展更多平台。

最终目标是构建一个可维护、可扩展、合规风险较低的多平台内容发布 Agent。

# TODO（已完成）

- [x] 允许用户自定义图片和视频存储目录。切换时先复制并校验已有素材，成功后再启用新目录，旧目录保留作为备份。
- [x] 发布 Agent 会监听平台页的发布结果；用户若已在浏览器中点击发布，中台会自动记录成功，再次确认发布也保持幂等。
- [x] 平台发布页中修改的标题和正文会自动同步到对应平台草稿，并在中台显示“平台页同步”。
- [x] 自动原图匹配失败后，可手动指定原图文件夹；系统会同步建立索引并绕过标签路由重新匹配。
- [x] 小红书与抖音链接支持空格或换行分隔的批量导入，单条失败不影响同批其他作品。
- [x] 已按后续需求移除微信朋友圈发布功能，历史记录保留用于状态修正或删除。
- [x] 批量导入链接增加横向进度条显示，下一行显示当前正在下载 的post名称 第[a]/[b]条 ，图片已下载 第[x]/[y]张。
- [x] Content editor里，下方的图片缩略图预览需要可以点击查看大图。操作逻辑是如果尚未匹配高清原图，就直接查看下载的图片。如果已经匹配到原图，就查看原图。
- [x] 主页“平台适配”按钮更改成“平台管理”，需要可点击。点击后可以按照平台查看 存放在 content hub 里 已经发布的作品。
- [x] 主页发布队列未实现.在本地编辑好后，需要支持多平台批量发布。同时带有发布进度条。
- [x] 豆包 prompt 带有tag生成。默认在正文末尾添加自动生成5个tag。
- [x] Content editor 添加按钮，当有对应小红书/抖音文案存在时，允许用户一键同步小红书/抖音文案。
- [x] Content editor 创建新内容并从本地上传文件时，是直接复制到对应的 32位GUID的文件夹。并且文件名也是变成了32位GUID。我需要文件名为 序号+源文件名的形式，并存储在originals文件夹。
- [x] 所有下载的媒体存储路径改为 32位GUID的文件夹\downloads。例如 \75fab010-5fa0-4ca8-b32b-0679e2844be1\downloads。移动所有媒体文件至新增加的文件夹路径，并且更新数据库。
- [x] 抖音平台发布准备页识别正文中的 #tag 与 @用户；Agent 自动输入并选择精确话题候选，并输入第一位 @用户、打开官方下拉列表供人工确认。小红书和 B 站暂时继续在官方页面人工处理。
- [x] 取消素材上传、离开抖音发布页或关闭浏览器页时，发布任务自动取消并释放平台锁，后续可直接重试，不再永久排队。
