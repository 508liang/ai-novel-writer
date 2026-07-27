"""
AI 网文写作系统 Agent v3.1

架构：
- 5个写作Agent（不同风格的作者）
- 1个审查Agent（API驱动，整体评估）
- 1个导演Agent（协调决策，控制节奏）
- 1个总编Agent（把握全局，调整故事，完善人物）
- 1个总导演Agent（规划主线，动态决策）

原则：
- 所有Agent都通过API运行
- 审查是整体评估，不是规则检查
- 提示词不限制具体词汇，而是描述写作方向
- 充分利用项目设定文件（characters.md, story_plan.md, outline/master.md）
- 并行写作 + 并行审查，最大化效率
- 支持暂停/继续，随时可以中断和恢复创作
"""

import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed


# ==================== GUI 回调系统 ====================

# GUI 模式下的回调函数（默认为 None，表示命令行模式）
_on_output: Optional[Callable[[str], None]] = None
_on_input: Optional[Callable[[str], str]] = None
_on_confirm: Optional[Callable[[str], bool]] = None


_original_print = print  # 保存原始 print


class _GuiOutputStream:
    """将 stdout 重定向到 GUI 回调"""

    def __init__(self):
        self._original = None

    def set_original(self, original):
        self._original = original

    def write(self, text):
        if text.strip() and _on_output:
            _on_output(text.rstrip('\n'))
        elif self._original:
            self._original.write(text)

    def flush(self):
        if self._original:
            self._original.flush()


_gui_stream = _GuiOutputStream()


def setup_gui_callbacks(
    on_output: Optional[Callable[[str], None]] = None,
    on_input: Optional[Callable[[str], str]] = None,
    on_confirm: Optional[Callable[[str], bool]] = None,
):
    """设置 GUI 回调函数，用于将输出/输入重定向到 GUI"""
    global _on_output, _on_input, _on_confirm
    _on_output = on_output
    _on_input = on_input
    _on_confirm = on_confirm

    # 重定向 stdout 以捕获所有 print 输出
    import sys
    if on_output:
        _gui_stream.set_original(sys.stdout)
        sys.stdout = _gui_stream
    elif _gui_stream._original:
        sys.stdout = _gui_stream._original


def gui_print(*args, **kwargs):
    """打印函数，支持 GUI 回调"""
    msg = " ".join(str(a) for a in args)
    if _on_output:
        _on_output(msg)
    else:
        print(*args, **kwargs)


def gui_input(prompt: str = "") -> str:
    """输入函数，支持 GUI 回调"""
    if _on_input:
        return _on_input(prompt)
    else:
        return input(prompt)


def gui_confirm(prompt: str) -> bool:
    """确认对话框，支持 GUI 回调"""
    if _on_confirm:
        return _on_confirm(prompt)
    else:
        return input(f"{prompt} (y/N): ").lower() == 'y'


def is_api_configured() -> bool:
    """检查 API 是否已配置"""
    api = CONFIG.get("api", {})
    return bool(api.get("url") and api.get("key"))


# ==================== 目录配置 ====================

PROJECT_DIR = Path(__file__).parent
CHAPTERS_DIR = PROJECT_DIR / "chapters"
RAW_DIR = PROJECT_DIR / "raw"
OUTLINE_DIR = PROJECT_DIR / "outline"
LOG_DIR = PROJECT_DIR / "logs"
NOTES_FILE = PROJECT_DIR / "writing_notes.json"
CONFIG_FILE = PROJECT_DIR / "config.json"

# 确保目录存在
CHAPTERS_DIR.mkdir(exist_ok=True)
RAW_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


# ==================== 配置系统 ====================

DEFAULT_CONFIG = {
    "api": {
        "url": "",
        "key": "",
        "model": "",
        "timeout": 300,
        "max_retries": 3
    },
    "writing": {
        "target_length": 15000,
        "max_rounds": 3,
        "chars_per_chapter": 5000,
        "max_events_per_volume": 15
    },
    "review": {
        "pass_score": 7.5,
        "dimensions": ["爽感密度", "设定自洽", "节奏张力", "人设一致", "叙事衔接", "追读引力"],
        "weights": {
            "opening": {"爽感密度": 2, "设定自洽": 1, "节奏张力": 2, "人设一致": 1, "叙事衔接": 2, "追读引力": 2},
            "rising": {"爽感密度": 2, "设定自洽": 1, "节奏张力": 1, "人设一致": 2, "叙事衔接": 1, "追读引力": 2},
            "climax": {"爽感密度": 3, "设定自洽": 1, "节奏张力": 2, "人设一致": 2, "叙事衔接": 1, "追读引力": 1},
            "daily": {"爽感密度": 1, "设定自洽": 2, "节奏张力": 1, "人设一致": 2, "叙事衔接": 2, "追读引力": 2}
        }
    },
    "output": {
        "show_progress": True,
        "save_raw": True,
        "auto_backup": True
    }
}

def load_config() -> dict:
    """加载配置文件，如果不存在则创建默认配置"""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # 合并默认配置（处理新增字段）
            merged = DEFAULT_CONFIG.copy()
            for key, value in config.items():
                if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                    merged[key].update(value)
                else:
                    merged[key] = value
            return merged
        except Exception as e:
            logger.warning(f"加载配置失败：{e}，使用默认配置")

    # 创建默认配置文件
    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG

def save_config(config: dict):
    """保存配置到文件"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

# 全局配置
CONFIG = load_config()


# ==================== 日志配置 ====================

log_filename = LOG_DIR / f"writing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ==================== 笔记系统 ====================

@dataclass
class WritingNotes:
    """创作笔记：记录故事进展，支持暂停/继续"""
    current_volume: int = 1
    current_event_index: int = 0
    total_chapters: int = 0
    total_words: int = 0
    events_completed: List[Dict] = field(default_factory=list)  # 已完成的事件
    active_plot_threads: List[str] = field(default_factory=list)  # 活跃伏笔
    character_states: Dict[str, str] = field(default_factory=dict)  # 角色状态
    recent_events: List[str] = field(default_factory=list)  # 最近发生的事件
    writing_decisions: List[str] = field(default_factory=list)  # 重要创作决定
    next_event_hint: str = ""  # 下一个事件的提示
    last_updated: str = ""  # 最后更新时间

    def save(self):
        """保存笔记到文件"""
        self.last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = {
            "current_volume": self.current_volume,
            "current_event_index": self.current_event_index,
            "total_chapters": self.total_chapters,
            "total_words": self.total_words,
            "events_completed": self.events_completed,
            "active_plot_threads": self.active_plot_threads,
            "character_states": self.character_states,
            "recent_events": self.recent_events,
            "writing_decisions": self.writing_decisions,
            "next_event_hint": self.next_event_hint,
            "last_updated": self.last_updated,
        }
        with open(NOTES_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"笔记已保存：{len(self.events_completed)}个事件，{self.total_words}字")

    @classmethod
    def load(cls) -> 'WritingNotes':
        """从文件加载笔记"""
        if not NOTES_FILE.exists():
            logger.info("未找到笔记文件，创建新笔记")
            return cls()

        try:
            with open(NOTES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            notes = cls()
            notes.current_volume = data.get("current_volume", 1)
            notes.current_event_index = data.get("current_event_index", 0)
            notes.total_chapters = data.get("total_chapters", 0)
            notes.total_words = data.get("total_words", 0)
            notes.events_completed = data.get("events_completed", [])
            notes.active_plot_threads = data.get("active_plot_threads", [])
            notes.character_states = data.get("character_states", {})
            notes.recent_events = data.get("recent_events", [])
            notes.writing_decisions = data.get("writing_decisions", [])
            notes.next_event_hint = data.get("next_event_hint", "")
            notes.last_updated = data.get("last_updated", "")

            logger.info(f"加载笔记成功：{len(notes.events_completed)}个事件，{notes.total_words}字")
            return notes
        except Exception as e:
            logger.error(f"加载笔记失败：{e}")
            return cls()

    def add_event(self, event_name: str, event_summary: str, chapter_count: int, word_count: int, chapter_files: List[str] = None):
        """
        记录一个完成的事件

        Args:
            event_name: 事件名称
            event_summary: 事件概述
            chapter_count: 章节数量
            word_count: 字数
            chapter_files: 生成的章节文件名列表
        """
        self.events_completed.append({
            "name": event_name,
            "summary": event_summary,
            "chapters": chapter_count,
            "words": word_count,
            "chapter_files": chapter_files or [],
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        self.total_chapters += chapter_count
        self.total_words += word_count
        self.current_event_index += 1
        self.recent_events.append(event_name)
        # 只保留最近10个事件
        if len(self.recent_events) > 10:
            self.recent_events = self.recent_events[-10:]

    def update_plot_threads(self, new_threads: List[str]):
        """更新伏笔"""
        for thread in new_threads:
            if thread not in self.active_plot_threads:
                self.active_plot_threads.append(thread)

    def update_character_state(self, character: str, state: str):
        """更新角色状态"""
        self.character_states[character] = state

    def get_summary(self) -> str:
        """获取笔记摘要"""
        lines = [
            f"当前进度：第{self.current_volume}卷，已写{len(self.events_completed)}个事件",
            f"总章节数：{self.total_chapters}章，总字数：{self.total_words}字",
            f"最后更新：{self.last_updated}",
        ]
        if self.recent_events:
            lines.append(f"最近事件：{'、'.join(self.recent_events[-5:])}")
        if self.active_plot_threads:
            lines.append(f"活跃伏笔：{'、'.join(self.active_plot_threads[:5])}")
        if self.next_event_hint:
            lines.append(f"下一事件：{self.next_event_hint}")
        return "\n".join(lines)


# ==================== API配置（从config加载） ====================

def get_api_config() -> dict:
    """获取API配置"""
    return CONFIG.get("api", DEFAULT_CONFIG["api"])

def get_writing_config() -> dict:
    """获取写作配置"""
    return CONFIG.get("writing", DEFAULT_CONFIG["writing"])

def get_review_config() -> dict:
    """获取审查配置"""
    return CONFIG.get("review", DEFAULT_CONFIG["review"])


# ==================== 数据结构 ====================

@dataclass
class ChapterContext:
    """章节上下文：传递给写作Agent的背景信息"""
    event_name: str
    event_summary: str
    target_length: int = None  # 从配置加载
    previous_text: str = ""  # 前文内容
    character_states: Dict[str, str] = field(default_factory=dict)  # 角色状态
    plot_threads: List[str] = field(default_factory=list)  # 伏笔线索
    writing_notes: str = ""  # 导演的写作指导
    story_stage: str = "opening"  # 故事阶段：opening, rising, climax, daily

    def __post_init__(self):
        if self.target_length is None:
            self.target_length = get_writing_config().get("target_length", 15000)


@dataclass
class ReviewResult:
    """审查结果"""
    score: float
    dimensions: Dict[str, dict]  # {维度名: {score, issue}}
    issues: List[str]
    suggestions: List[str]
    summary: str
    passed: bool  # 是否通过
    weighted_score: float = 0.0  # 加权分数


@dataclass
class WritingVersion:
    """写作版本"""
    agent_name: str
    content: str
    word_count: int
    version_id: int


# ==================== API封装 ====================

def call_api(system_prompt: str, user_prompt: str, max_tokens: int = 16000, temperature: float = 0.8, enable_thinking: bool = False, timeout: int = 300) -> str:
    """
    调用API，带重试机制

    Args:
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        max_tokens: 最大token数（默认16000，支持长文本生成）
        temperature: 温度参数
        enable_thinking: 是否启用思考模式（默认关闭，更快）
        timeout: 超时时间（秒）

    Returns:
        API返回的内容，失败返回空字符串
    """
    api_config = get_api_config()
    if not api_config.get("url") or not api_config.get("key"):
        logger.error("API 未配置：请先在设置中填写 API 地址和密钥")
        gui_print("[X] API 未配置，请先在设置中填写 API 地址和密钥")
        return ""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_config['key']}"}
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    data = {
        "model": api_config["model"],
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": temperature,
        "thinking": {"type": "enabled" if enable_thinking else "disabled"}
    }

    max_retries = api_config.get("max_retries", 3)
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                api_config["url"],
                data=json.dumps(data).encode('utf-8'),
                headers=headers,
                method='POST'
            )
            actual_timeout = timeout if timeout != 300 else api_config.get("timeout", 300)
            with urllib.request.urlopen(req, timeout=actual_timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
                message = result['choices'][0]['message']
                content = message.get('content', '') or message.get('reasoning_content', '')
                if content:
                    logger.debug(f"API调用成功，返回{len(content)}字")
                    return content
        except Exception as e:
            logger.warning(f"API调用失败（第{attempt+1}次）：{str(e)[:100]}")
            if attempt < max_retries - 1:
                time.sleep(2)  # 等待2秒后重试
                continue
    logger.error(f"API调用{max_retries}次全部失败")
    return ""


def load_project_file(filename: str) -> str:
    """加载项目设定文件"""
    filepath = PROJECT_DIR / filename
    if filepath.exists():
        return filepath.read_text(encoding='utf-8')
    return ""


# ==================== 设定加载器 ====================

class StoryContext:
    """故事上下文：加载和管理项目设定（带缓存）"""

    def __init__(self):
        self.characters = load_project_file("characters.md")
        self.story_plan = load_project_file("story_plan.md")
        self.master_outline = load_project_file("outline/master.md")
        self.tracking = load_project_file("tracking.md")

        # 缓存
        self._character_brief_cache: Optional[str] = None
        self._worldview_brief_cache: Optional[str] = None

    def get_character_brief(self) -> str:
        """获取角色简要信息（用于提示词）- 带缓存"""
        if self._character_brief_cache is not None:
            return self._character_brief_cache

        if not self.characters:
            self._character_brief_cache = ""
            return ""

        # 提取关键角色信息，去掉过长的描述
        lines = self.characters.split('\n')
        brief = []
        in_character = False
        for line in lines:
            if line.startswith('### '):
                in_character = True
                brief.append(line)
            elif in_character and line.startswith('- **'):
                brief.append(line)
            elif line.startswith('## ') and not line.startswith('### '):
                in_character = False

        self._character_brief_cache = '\n'.join(brief[:50])
        return self._character_brief_cache

    def get_worldview_brief(self) -> str:
        """获取世界观简要信息"""
        if not self.master_outline:
            return ""
        # 提取世界观部分
        lines = self.master_outline.split('\n')
        brief = []
        in_worldview = False
        for line in lines:
            if '世界观' in line or '修行体系' in line or '末劫' in line:
                in_worldview = True
            if in_worldview:
                brief.append(line)
                if line.startswith('## ') and '世界观' not in line:
                    break
        return '\n'.join(brief[:30])

    def get_current_progress(self) -> str:
        """获取当前进度"""
        if not self.tracking:
            return ""
        return self.tracking[:500]  # 限制长度


# ==================== 写作Agent ====================
# 5个写作Agent，每个都是独立的"作者"，有自己的写作风格
# 不是按维度分工，而是各有特色，最后审查选最好的


class BaseWriterAgent:
    """写作Agent基类"""

    def _build_context(self, ctx: ChapterContext) -> str:
        """构建上下文信息"""
        parts = []
        if ctx.character_states:
            parts.append("当前角色状态：" + json.dumps(ctx.character_states, ensure_ascii=False))
        if ctx.plot_threads:
            parts.append("活跃伏笔：" + "、".join(ctx.plot_threads))
        return "\n".join(parts) if parts else "（无额外上下文）"

    def _call_write_api(self, system_prompt: str, ctx: ChapterContext, version: int, agent_name: str) -> WritingVersion:
        """
        调用写作API

        注意：system_prompt 应该已经完成格式化，不要在这里二次格式化
        """
        # 计算实际需要的token数（1个中文字约2-3个token）
        max_tokens = min(ctx.target_length * 3, 64000)  # 限制最大64k tokens

        user_prompt = f"""请为以下事件写小说内容。

【重要要求】目标字数：{ctx.target_length}字
请务必写够字数，不要提前结束。每写5000字左右可以有一个小节，但要继续写下去，直到达到目标字数。

事件：{ctx.event_summary}

{f'前文内容（最后500字）：{ctx.previous_text[-500:]}' if ctx.previous_text else ''}
{f'写作指导：{ctx.writing_notes}' if ctx.writing_notes else ''}

这是第{version}个版本，请按照你的风格自由发挥。

直接输出小说正文，不要任何标记、标题或说明。请开始写作："""

        content = call_api(system_prompt, user_prompt, max_tokens)

        # 扩写：如果字数不够，从头重写并要求更多字数
        current_length = len(content)
        target = ctx.target_length
        max_rounds = 3  # 最多扩写3次

        round_num = 1
        while current_length < target * 0.8 and round_num < max_rounds:
            round_num += 1
            logger.info(f"{agent_name}扩写第{round_num}轮（当前{current_length}字，目标{target}字）")

            # 扩写：从头重写，要求更多字数
            expand_prompt = f"""请重新写这个故事，要求字数更多（目标{target}字）。

事件：{ctx.event_summary}

你之前写的版本（{current_length}字）：
{content[:2000]}...

请重新写一个更完整的版本，包含更多细节、对话、场景描写，达到{target}字。

直接输出小说正文，不要任何标记、标题或说明。"""

            new_content = call_api(system_prompt, expand_prompt, max_tokens)
            if new_content and len(new_content) > current_length:
                content = new_content
                current_length = len(content)
            else:
                break

        return WritingVersion(
            agent_name=agent_name,
            content=content,
            word_count=len(content),
            version_id=version
        )


class WriterAgentA(BaseWriterAgent):
    """
    作者A：细腻派（笔名：细雨）
    擅长：环境描写、氛围营造、情感细腻
    风格：散文式，注重感官细节，节奏舒缓
    """

    SYSTEM_PROMPT = """你是AI网文写作系统的写作者，笔名"细雨"。

你的写作风格：
- 像散文一样自然展开，注重意境营造
- 善用感官细节（视觉、听觉、触觉、嗅觉）
- 情感表达含蓄内敛，点到为止
- 节奏偏慢，但每一句都有存在价值
- 开头喜欢从一个小细节切入，逐渐展开

写作要求：
- 像真实的小说作者一样写作，不要像AI
- 直接写故事，不要解释你在做什么
- 让读者沉浸在故事中，忘记这是AI写的

质量要求（审查标准）：
1. 爽感密度：要有冲突、情感波动、进展
2. 设定自洽：前后一致，世界观自洽
3. 节奏张力：张弛有度，不拖沓
4. 人设一致：人物行为符合性格
5. 叙事衔接：场景转换自然，开头结尾有力
6. 追读引力：有悬念，让人想继续读

{context_info}

角色设定参考：
{character_styles}"""

    def __init__(self, story_context: StoryContext):
        self.character_styles = story_context.get_character_brief()[:800] if story_context else ""

    def write(self, ctx: ChapterContext, version: int) -> WritingVersion:
        system_prompt = self.SYSTEM_PROMPT.format(
            context_info=self._build_context(ctx),
            character_styles=self.character_styles
        )
        return self._call_write_api(system_prompt, ctx, version, "细雨")


class WriterAgentB(BaseWriterAgent):
    """
    作者B：冲突派（笔名：烈火）
    擅长：制造冲突、节奏明快、爽感强烈
    风格：快节奏，冲突密集，情绪起伏大
    """

    SYSTEM_PROMPT = """你是AI网文写作系统的写作者，笔名"烈火"。

你的写作风格：
- 快节奏，不拖泥带水
- 善于制造冲突和矛盾，让故事有张力
- 情绪起伏大，先压抑后释放
- 喜欢用短句，节奏感强
- 结尾喜欢留悬念，让人想继续看

写作要求：
- 像真实的小说作者一样写作，不要像AI
- 直接写故事，不要解释你在做什么
- 让读者停不下来，一直想看下去

质量要求（审查标准）：
1. 爽感密度：要有冲突、情感波动、进展
2. 设定自洽：前后一致，世界观自洽
3. 节奏张力：张弛有度，不拖沓
4. 人设一致：人物行为符合性格
5. 叙事衔接：场景转换自然，开头结尾有力
6. 追读引力：有悬念，让人想继续读

{context_info}

角色设定参考：
{character_styles}"""

    def __init__(self, story_context: StoryContext):
        self.character_styles = story_context.get_character_brief()[:800] if story_context else ""

    def write(self, ctx: ChapterContext, version: int) -> WritingVersion:
        system_prompt = self.SYSTEM_PROMPT.format(
            context_info=self._build_context(ctx),
            character_styles=self.character_styles
        )
        return self._call_write_api(system_prompt, ctx, version, "烈火")


class WriterAgentC(BaseWriterAgent):
    """
    作者C：对话派（笔名：清风）
    擅长：人物对话、性格刻画、关系描写
    风格：对话驱动，人物鲜活，互动有趣
    """

    SYSTEM_PROMPT = """你是AI网文写作系统的写作者，笔名"清风"。

你的写作风格：
- 对话驱动故事，人物通过对话展现性格
- 每个角色说话方式独特，有记忆点
- 善于写潜台词，话里有话
- 人物互动有趣，关系描写细腻
- 节奏适中，张弛有度

写作要求：
- 像真实的小说作者一样写作，不要像AI
- 直接写故事，不要解释你在做什么
- 让读者记住这些角色，关心他们的命运

质量要求（审查标准）：
1. 爽感密度：要有冲突、情感波动、进展
2. 设定自洽：前后一致，世界观自洽
3. 节奏张力：张弛有度，不拖沓
4. 人设一致：人物行为符合性格
5. 叙事衔接：场景转换自然，开头结尾有力
6. 追读引力：有悬念，让人想继续读

{context_info}

角色说话风格参考：
{character_styles}"""

    def __init__(self, story_context: StoryContext):
        self.character_styles = story_context.get_character_brief()[:1000] if story_context else ""

    def write(self, ctx: ChapterContext, version: int) -> WritingVersion:
        system_prompt = self.SYSTEM_PROMPT.format(
            context_info=self._build_context(ctx),
            character_styles=self.character_styles
        )
        return self._call_write_api(system_prompt, ctx, version, "清风")


class WriterAgentD(BaseWriterAgent):
    """
    作者D：细节派（笔名：磐石）
    擅长：世界观展现、设定严谨、逻辑自洽
    风格：细节丰富，设定清晰，逻辑严密
    """

    SYSTEM_PROMPT = """你是AI网文写作系统的写作者，笔名"磐石"。

你的写作风格：
- 世界观展现自然，通过情节带出设定
- 细节严谨，前后呼应，没有漏洞
- 人物行为符合逻辑，不强行降智
- 善于通过小细节展现大世界
- 节奏稳健，步步为营

写作要求：
- 像真实的小说作者一样写作，不要像AI
- 直接写故事，不要解释你在做什么
- 让读者相信这个世界是真实的

质量要求（审查标准）：
1. 爽感密度：要有冲突、情感波动、进展
2. 设定自洽：前后一致，世界观自洽
3. 节奏张力：张弛有度，不拖沓
4. 人设一致：人物行为符合性格
5. 叙事衔接：场景转换自然，开头结尾有力
6. 追读引力：有悬念，让人想继续读

{context_info}

角色设定参考：
{character_styles}"""

    def __init__(self, story_context: StoryContext):
        self.character_styles = story_context.get_character_brief()[:1000] if story_context else ""

    def write(self, ctx: ChapterContext, version: int) -> WritingVersion:
        system_prompt = self.SYSTEM_PROMPT.format(
            context_info=self._build_context(ctx),
            character_styles=self.character_styles
        )
        return self._call_write_api(system_prompt, ctx, version, "磐石")


class WriterAgentE(BaseWriterAgent):
    """
    作者E：悬念派（笔名：迷雾）
    擅长：悬念设置、钩子设计、追读引力
    风格：悬念不断，钩子密集，让人欲罢不能
    """

    SYSTEM_PROMPT = """你是AI网文写作系统的写作者，笔名"迷雾"。

你的写作风格：
- 善于设置悬念，让读者好奇
- 钩子设计巧妙，让人欲罢不能
- 信息控制得当，该藏的藏，该露的露
- 节奏有起伏，张弛有度
- 结尾总是让人想看下一章

写作要求：
- 像真实的小说作者一样写作，不要像AI
- 直接写故事，不要解释你在做什么
- 让读者猜不到接下来会发生什么

质量要求（审查标准）：
1. 爽感密度：要有冲突、情感波动、进展
2. 设定自洽：前后一致，世界观自洽
3. 节奏张力：张弛有度，不拖沓
4. 人设一致：人物行为符合性格
5. 叙事衔接：场景转换自然，开头结尾有力
6. 追读引力：有悬念，让人想继续读

{context_info}

角色设定参考：
{character_styles}"""

    def __init__(self, story_context: StoryContext):
        self.character_styles = story_context.get_character_brief()[:800] if story_context else ""

    def write(self, ctx: ChapterContext, version: int) -> WritingVersion:
        system_prompt = self.SYSTEM_PROMPT.format(
            context_info=self._build_context(ctx),
            character_styles=self.character_styles
        )
        return self._call_write_api(system_prompt, ctx, version, "迷雾")


# ==================== 审查Agent ====================

class ReviewAgent:
    """审查Agent：六维度评估，整体审查，支持动态权重"""

    SYSTEM_PROMPT = """你是AI网文写作系统的审查AI，负责整体评估小说质量。

审查标准（六维度，每维度1-10分）：

1. **爽感密度**：有没有冲突、情感波动、进展？读者能不能获得情感满足？
2. **设定自洽**：前后是否一致？有没有矛盾？世界观是否自洽？
3. **节奏张力**：叙事节奏是否合适？张弛有度？有没有拖沓或跳跃？
4. **人设一致**：人物行为是否符合性格？对话是否符合角色说话方式？
5. **叙事衔接**：场景转换是否自然？开头结尾是否有力？与前文是否连贯？
6. **追读引力**：是否有悬念？是否让人想继续读？有没有"钩子"？

评分标准：
- 7分以上：合格，可以使用
- 8分以上：良好
- 9分以上：优秀
- 严格评分，不要虚高

{story_context}"""

    def __init__(self, story_context: StoryContext):
        self.story_context = story_context
        self.review_config = get_review_config()

    def _get_weights(self, story_stage: str) -> Dict[str, float]:
        """获取当前故事阶段的审查权重"""
        weights_config = self.review_config.get("weights", {})
        return weights_config.get(story_stage, weights_config.get("daily", {}))

    def _calculate_weighted_score(self, dimensions: Dict[str, dict], story_stage: str) -> float:
        """计算加权分数"""
        weights = self._get_weights(story_stage)
        if not weights:
            # 没有权重配置，使用平均分
            scores = [d["score"] for d in dimensions.values()]
            return sum(scores) / len(scores) if scores else 0

        total_weight = 0
        weighted_sum = 0
        for dim_name, dim_data in dimensions.items():
            weight = weights.get(dim_name, 1)
            weighted_sum += dim_data["score"] * weight
            total_weight += weight

        return round(weighted_sum / total_weight, 1) if total_weight > 0 else 0

    def review(self, text: str, ctx: ChapterContext) -> ReviewResult:
        """六维度审查，支持动态权重"""
        story_context_info = self._get_story_context()
        system_prompt = self.SYSTEM_PROMPT.format(story_context=story_context_info)

        user_prompt = f"""请对以下小说片段进行六维度评分：

事件：{ctx.event_summary}

文本内容：
{text[:3000]}

请严格按JSON格式输出：
{{
  "爽感密度": {{"score": 7, "issue": "问题或无"}},
  "设定自洽": {{"score": 7, "issue": "问题或无"}},
  "节奏张力": {{"score": 7, "issue": "问题或无"}},
  "人设一致": {{"score": 7, "issue": "问题或无"}},
  "叙事衔接": {{"score": 7, "issue": "问题或无"}},
  "追读引力": {{"score": 7, "issue": "问题或无"}},
  "suggestions": ["改进建议1", "改进建议2"]
}}"""

        result = call_api(system_prompt, user_prompt, 800, temperature=0.3)  # 低温度，稳定输出
        if not result:
            print("     警告：审查API返回空内容")
        return self._parse_review(result, ctx.story_stage)

    def _get_story_context(self) -> str:
        """获取故事上下文"""
        parts = []
        chars = self.story_context.get_character_brief()
        if chars:
            parts.append("角色设定参考：\n" + chars[:500])
        progress = self.story_context.get_current_progress()
        if progress:
            parts.append("当前进度：\n" + progress[:300])
        return "\n\n".join(parts) if parts else ""

    def _parse_review(self, result: str, story_stage: str = "daily") -> ReviewResult:
        """解析审查结果，支持动态权重"""
        if not result:
            return self._default_result("API返回空内容")

        try:
            # 使用更精确的匹配：找到最后一个完整的JSON对象
            # 从后往前搜索，找到最后一个 }
            last_brace = result.rfind('}')
            if last_brace == -1:
                print(f"     警告：未找到JSON格式")
                return self._default_result("未找到JSON")

            # 从最后一个}往前找到匹配的{
            brace_count = 0
            start_pos = last_brace
            for i in range(last_brace, -1, -1):
                if result[i] == '}':
                    brace_count += 1
                elif result[i] == '{':
                    brace_count -= 1
                if brace_count == 0:
                    start_pos = i
                    break

            json_str = result[start_pos:last_brace + 1]

            # 清理JSON字符串
            json_str = json_str.replace('\n', ' ').replace('\r', '')
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)

            data = json.loads(json_str)

            dimensions = {}
            issues = []
            suggestions = []
            total_score = 0
            count = 0

            for dim_name in ['爽感密度', '设定自洽', '节奏张力', '人设一致', '叙事衔接', '追读引力']:
                if dim_name in data:
                    dim = data[dim_name]
                    score = dim.get('score', 5)
                    issue = dim.get('issue', '')
                    dimensions[dim_name] = {"score": score, "issue": issue}
                    total_score += score
                    count += 1
                    if issue and issue != '无' and issue != '问题或无':
                        issues.append(f"{dim_name}：{issue}")

            suggestions = data.get('suggestions', [])

            if count > 0:
                avg_score = round(total_score / count, 1)
                # 计算加权分数
                weighted_score = self._calculate_weighted_score(dimensions, story_stage)
                pass_score = self.review_config.get("pass_score", 7.5)

                # 使用加权分数作为最终分数
                final_score = weighted_score if weighted_score > 0 else avg_score

                return ReviewResult(
                    score=final_score,
                    dimensions=dimensions,
                    issues=issues,
                    suggestions=suggestions,
                    summary=f"加权{final_score}分（平均{avg_score}分）",
                    passed=final_score >= pass_score,
                    weighted_score=weighted_score
                )
        except json.JSONDecodeError as e:
            print(f"     警告：JSON解析错误：{e}")
        except Exception as e:
            print(f"     警告：解析异常：{e}")

        # 解析失败
        return self._default_result("解析失败")

    def _default_result(self, reason: str) -> ReviewResult:
        """返回默认的审查结果"""
        return ReviewResult(
            score=5.0,
            dimensions={},
            issues=[f"审查{reason}"],
            suggestions=["需要人工检查"],
            summary=reason,
            passed=False
        )

    def review_parallel(self, versions: List[WritingVersion], ctx: ChapterContext) -> List[Tuple[WritingVersion, ReviewResult]]:
        """并行审查多个版本（最多5个）"""
        results = []

        def review_one(version: WritingVersion) -> Tuple[WritingVersion, ReviewResult]:
            review = self.review(version.content, ctx)
            return (version, review)

        # 使用5个worker来并行审查5个版本
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(review_one, v) for v in versions]
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.error(f"审查异常：{e}")

        return results


# ==================== 导演Agent ====================

class DirectorAgent:
    """导演Agent：协调决策，控制节奏"""

    SYSTEM_PROMPT = """你是AI网文写作系统的导演AI，负责协调写作和控制节奏。

你的职责：
1. 根据审查结果，决定是否通过当前版本
2. 如果不通过，给出具体的改进方向
3. 协调三个写作Agent的输出，选择最佳版本或融合优点
4. 控制整体节奏，确保故事流畅

决策原则：
- 7.5分以上可以考虑通过
- 8分以上直接通过
- 如果所有版本都不够好，给出具体改进方向
- 优先选择最符合故事氛围的版本

{story_context}"""

    def __init__(self, story_context: StoryContext):
        self.story_context = story_context

    def _get_story_context(self) -> str:
        """获取故事上下文"""
        parts = []
        chars = self.story_context.get_character_brief()
        if chars:
            parts.append("角色设定参考：\n" + chars[:500])
        return "\n\n".join(parts) if parts else ""

    def decide(self, review_results: List[Tuple[WritingVersion, ReviewResult]], _ctx: ChapterContext) -> Tuple[Optional[WritingVersion], str]:
        """
        根据审查结果做决策

        Returns:
            (最佳版本, 改进指导) - 如果通过，返回最佳版本和空指导；如果不通过，返回None和改进指导
        """
        if not review_results:
            return None, "所有版本生成失败，请重试"

        # 按分数排序
        sorted_results = sorted(review_results, key=lambda x: x[1].score, reverse=True)
        best_version, best_review = sorted_results[0]

        # 检查是否通过
        if best_review.passed:
            return best_version, ""

        # 不通过，生成改进指导
        story_context_info = self._get_story_context()
        system_prompt = self.SYSTEM_PROMPT.format(story_context=story_context_info)

        # 收集所有问题
        all_issues = []
        all_suggestions = []
        for _, review in sorted_results:
            all_issues.extend(review.issues)
            all_suggestions.extend(review.suggestions)

        # 去重
        unique_issues = list(set(all_issues))[:5]
        unique_suggestions = list(set(all_suggestions))[:3]

        # 构建详细的审查反馈
        dimension_feedback = []
        for dim_name, dim_data in best_review.dimensions.items():
            score = dim_data.get("score", 0)
            issue = dim_data.get("issue", "")
            if issue and issue != '无':
                dimension_feedback.append(f"- {dim_name}：{score}分 - {issue}")
            else:
                dimension_feedback.append(f"- {dim_name}：{score}分")

        user_prompt = f"""当前最佳版本评分：{best_review.score}分（需要达到7.5分才能通过）

各维度评分：
{chr(10).join(dimension_feedback)}

主要问题：
{chr(10).join(f'- {issue}' for issue in unique_issues)}

改进建议：
{chr(10).join(f'- {s}' for s in unique_suggestions)}

请给出具体的改进方向，告诉写作Agent应该如何改进，重点提升低分维度。"""

        guidance = call_api(system_prompt, user_prompt, 800, temperature=0.5)
        return None, guidance or "请改进以下问题：" + "、".join(unique_issues[:3])


# ==================== 总编Agent ====================

class EditorInChiefAgent:
    """总编Agent：把握全局，调整故事，完善人物"""

    SYSTEM_PROMPT = """你是AI网文写作系统的总编AI，负责把握全局和完善作品。

你的职责：
1. 审查最终版本，确保与整体故事一致
2. 调整人物表现，确保性格一致
3. 补充重要细节，完善伏笔和铺垫
4. 优化文字表达，提升文学质量

审核重点：
- 人物是否符合设定（参考角色设定）
- 是否与前文连贯
- 伏笔是否合理
- 文字是否流畅

{story_context}

{character_info}"""

    def __init__(self, story_context: StoryContext):
        self.story_context = story_context

    def polish(self, text: str, ctx: ChapterContext) -> str:
        """润色和完善最终版本"""
        logger.info(f"总编Agent开始润色（原文{len(text)}字）")

        story_context_info = self._get_story_context()
        character_info = "角色设定参考：\n" + self.story_context.get_character_brief()[:800]

        system_prompt = self.SYSTEM_PROMPT.format(
            story_context=story_context_info,
            character_info=character_info
        )

        user_prompt = f"""请润色以下小说，确保质量：

事件：{ctx.event_summary}

原文（{len(text)}字）：
{text}

润色要求：
1. 保持原文的核心内容和情感
2. 修正不自然的表达
3. 确保人物性格一致
4. 优化文字流畅度
5. 如有需要，补充重要细节
6. 保持原有字数，不要大幅删减
7. 只输出本章节内容，不要添加其他章节

直接输出润色后的正文，不要任何说明。"""

        max_tokens = min(len(text) * 3, 64000)  # 限制最大64k tokens
        result = call_api(system_prompt, user_prompt, max_tokens, temperature=0.6)

        if result:
            logger.info(f"润色完成（{len(result)}字）")
            # 如果润色后字数差异太大，返回原文
            if len(result) < len(text) * 0.5:
                logger.warning(f"润色后字数过少（{len(result)}字），返回原文")
                return text
            return result
        else:
            logger.warning("润色失败，返回原文")
            return text

    def analyze_chapter(self, text: str, ctx: ChapterContext) -> Dict:
        """分析章节，提取关键信息用于后续写作"""
        system_prompt = """你是AI网文写作系统的分析AI，负责分析章节内容。

请分析以下内容，提取：
1. 出现的角色及其状态变化
2. 新埋设的伏笔
3. 情节进展
4. 需要在后续注意的事项

以JSON格式输出。"""

        user_prompt = f"""请分析以下章节：

事件：{ctx.event_summary}

内容：
{text[:3000]}

JSON格式：
{{
  "characters": {{"角色名": "状态描述"}},
  "plot_threads": ["伏笔1", "伏笔2"],
  "plot_progress": "情节进展描述",
  "notes": "后续注意事项"
}}"""

        result = call_api(system_prompt, user_prompt, 500, temperature=0.3)
        try:
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except:
            pass
        return {}

    def _get_story_context(self) -> str:
        """获取故事上下文"""
        parts = []
        outline = self.story_context.master_outline
        if outline:
            parts.append("故事大纲参考：\n" + outline[:600])
        return "\n\n".join(parts) if parts else ""


# ==================== 主控制器 ====================

class NovelWritingSystem:
    """小说写作系统：协调所有Agent"""

    def __init__(self):
        # 加载故事上下文
        self.story_context = StoryContext()

        # 初始化5个写作Agent（5个不同风格的作者）
        self.writer_a = WriterAgentA(self.story_context)  # 细雨：细腻派
        self.writer_b = WriterAgentB(self.story_context)  # 烈火：冲突派
        self.writer_c = WriterAgentC(self.story_context)  # 清风：对话派
        self.writer_d = WriterAgentD(self.story_context)  # 磐石：细节派
        self.writer_e = WriterAgentE(self.story_context)  # 迷雾：悬念派

        # 初始化其他Agent
        self.reviewer = ReviewAgent(self.story_context)
        self.director = DirectorAgent(self.story_context)
        self.editor = EditorInChiefAgent(self.story_context)
        self.master_director = MasterDirectorAgent(self.story_context)
        self.assistant = AssistantAgent(self.story_context)

        # 加载笔记系统
        self.notes = WritingNotes.load()

        # 当前状态
        self.current_context: Optional[ChapterContext] = None
        # 基于现有文件数量初始化章节计数
        existing_chapters = list(CHAPTERS_DIR.glob("ch*.md"))
        self.chapter_count = len(existing_chapters)

        # 章节摘要记录
        self.chapter_summaries: List[str] = self._load_chapter_summaries()

        logger.info(f"写作系统初始化完成，已有{self.chapter_count}章")
        logger.info(f"笔记状态：{self.notes.get_summary()}")

    def _load_chapter_summaries(self) -> List[str]:
        """加载已有章节的摘要"""
        summaries = []
        chapter_files = sorted(CHAPTERS_DIR.glob("ch*.md"))
        for ch_file in chapter_files:
            try:
                content = ch_file.read_text(encoding='utf-8')
                summary = content[:100].replace('\n', ' ')
                summaries.append(f"{ch_file.name}: {summary}")
            except Exception:
                pass
        return summaries

    def run_event(self, event_name: str, event_summary: str, target_length: int = 15000, max_rounds: int = 3) -> str:
        """
        运行一个事件

        Args:
            event_name: 事件名称
            event_summary: 事件概述
            target_length: 目标字数
            max_rounds: 最大轮次

        Returns:
            最终文本
        """
        logger.info(f"开始写作事件：{event_name}")
        logger.info(f"事件概述：{event_summary}")
        logger.info(f"目标字数：{target_length}")

        print(f"\n{'='*60}")
        print(f"[书] 事件：{event_name}")
        print(f"[笔] 概述：{event_summary}")
        print(f"[目标] 目标：{target_length}字")
        print(f"{'='*60}\n")

        # 创建上下文
        ctx = ChapterContext(
            event_name=event_name,
            event_summary=event_summary,
            target_length=target_length
        )

        # 获取前文上下文
        ctx.previous_text = self._get_previous_text()
        ctx.character_states = self._get_character_states()
        ctx.plot_threads = self._get_plot_threads()

        best_text = ""
        review_results = []

        for round_num in range(max_rounds):
            # 检查停止标志
            try:
                from gui import _stop_event
                if _stop_event.is_set():
                    logger.info("收到停止信号，中断写作")
                    print("[!] 收到停止信号，正在保存当前进度...")
                    break
            except ImportError:
                pass

            logger.info(f"第{round_num + 1}轮写作")
            print(f"\n[轮] 第{round_num + 1}轮写作")
            print("-" * 40)

            # 1. 并行写作（3个Agent）
            print("[写]  三个写作Agent并行创作...")
            versions = self._write_parallel(ctx, round_num + 1)

            if not versions:
                logger.warning("所有版本生成失败")
                print("[X] 所有版本生成失败，重试...")
                continue

            print(f"[OK] 生成{len(versions)}个版本")

            # 2. 并行审查
            print("\n[查] 审查Agent评估中...")
            review_results = self.reviewer.review_parallel(versions, ctx)

            # 显示审查结果
            for version, review in review_results:
                status = "[OK]" if review.passed else "[WARN]" if review.score >= 6 else "[X]"
                print(f"  {status} {version.agent_name}（版本{version.version_id}）：{review.score}分，{version.word_count}字")
                logger.info(f"审查结果：{version.agent_name}（版本{version.version_id}）={review.score}分")
                # 显示各维度分数
                if review.dimensions:
                    dim_str = " | ".join([f"{k}:{v['score']}" for k, v in review.dimensions.items()])
                    print(f"     维度：{dim_str}")
                if review.issues:
                    for issue in review.issues[:2]:
                        print(f"     问题：{issue}")

            # 3. 导演决策
            print("\n[导] 导演Agent决策...")
            best_version, guidance = self.director.decide(review_results, ctx)

            if best_version:
                logger.info(f"通过！选择：{best_version.agent_name}（{best_version.word_count}字）")
                print(f"[OK] 通过！选择：{best_version.agent_name}（{best_version.word_count}字）")
                best_text = best_version.content
                break
            else:
                logger.info(f"未通过，继续改进")
                print(f"[WARN] 未通过，继续改进...")
                if guidance:
                    print(f"[笔] 改进方向：{guidance[:100]}...")
                ctx.writing_notes = guidance

        if not best_text:
            # 选择所有轮次中最高分的版本
            logger.warning("未能生成满意版本，使用最高分版本")
            print("\n[X] 未能生成满意版本，使用最高分版本")
            if review_results:
                # 按分数排序，选择最高分
                sorted_results = sorted(review_results, key=lambda x: x[1].score, reverse=True)
                best_version, best_review = sorted_results[0]
                best_text = best_version.content
                logger.info(f"选择最高分版本：{best_version.agent_name}（{best_review.score}分）")
                print(f"  选择：{best_version.agent_name}（{best_review.score}分）")

        # 检查是否真的有内容
        if not best_text or len(best_text) < 100:
            logger.error(f"事件 {event_name} 生成失败：无有效内容")
            print(f"\n[X] 事件 {event_name} 生成失败：所有写作Agent都未能生成有效内容")
            print(f" 建议：检查API连接或稍后重试")
            return ""

        # 直接使用最佳版本（不再润色）
        final_text = best_text

        # 4. 分析章节
        print("[析] 分析章节内容...")
        analysis = self.editor.analyze_chapter(final_text, ctx)

        # 5. 保存结果（一个事件 = 一章，不拆分）
        chapter_file, actual_chars = self._save_chapter(final_text, event_name)

        # 6. 更新追踪（使用助手Agent）
        self._update_tracking(analysis, event_name, event_summary, [chapter_file])

        # 7. 生成章节摘要
        summary = self.master_director.summarize_chapter(final_text, event_name)
        self.chapter_summaries.append(summary)
        logger.info(f"章节摘要：{summary}")

        # 8. 更新笔记
        self.notes.add_event(
            event_name, event_summary,
            1, actual_chars,  # 使用实际字数
            chapter_files=[chapter_file]
        )
        if analysis:
            if 'plot_threads' in analysis:
                self.notes.update_plot_threads(analysis['plot_threads'])
            if 'characters' in analysis:
                for char, state in analysis['characters'].items():
                    self.notes.update_character_state(char, state)
        self.notes.save()

        logger.info(f"事件完成：{event_name}（{actual_chars}字）")
        print(f"\n{'='*60}")
        print(f"[OK] 完成：{event_name}（{actual_chars}字）")
        print(f"[笔] 笔记已更新：{self.notes.get_summary()}")
        print(f"{'='*60}\n")

        return final_text

    def run_volume(self, volume: int, max_events: int = 15):
        """
        运行一卷，由总导演动态规划事件
        支持暂停/继续：中断后再次运行会从上次的位置继续

        Args:
            volume: 卷号
            max_events: 最大事件数
        """
        # 更新笔记中的卷号
        self.notes.current_volume = volume

        logger.info(f"开始写作第{volume}卷")
        print(f"\n{'='*60}")
        print(f"[卷] 开始写作第{volume}卷")
        print(f"{'='*60}")

        # 显示当前进度
        if self.notes.events_completed:
            print(f"[记] 从上次中断处继续：已完成{len(self.notes.events_completed)}个事件")
            print(f"[析] 当前进度：{self.notes.get_summary()}")
        print()

        # 从笔记中的位置继续
        start_index = self.notes.current_event_index

        for event_num in range(start_index, max_events):
            # 检查停止标志
            try:
                from gui import _stop_event
                if _stop_event.is_set():
                    logger.info("收到停止信号，中断写作")
                    print("[!] 收到停止信号，保存进度后停止...")
                    self.notes.current_event_index = event_num
                    self.notes.save()
                    break
            except ImportError:
                pass

            logger.info(f"规划第{event_num + 1}个事件")
            print(f"\n{'='*40}")
            print(f"[记] 事件 {event_num + 1}/{max_events}")
            print(f"{'='*40}")

            # 总导演决定下一个事件
            event_name, event_summary = self.master_director.decide_next_event(
                volume, self.chapter_summaries
            )

            # 运行事件
            try:
                result = self.run_event(event_name, event_summary, target_length=15000, max_rounds=3)
                logger.info(f"事件完成：{event_name}（{len(result)}字）")

                # 每个事件完成后显示进度
                print(f"\n[析] 当前进度：{self.notes.get_summary()}")

            except KeyboardInterrupt:
                # 用户中断，保存笔记
                logger.info("用户中断，保存笔记...")
                self.notes.save()
                print(f"\n[停]  已暂停！笔记已保存。")
                print(f"[析] 中断时进度：{self.notes.get_summary()}")
                print(f" 下次运行将继续从此处开始")
                return

            except Exception as e:
                logger.error(f"事件失败：{event_name}（{str(e)[:100]}）")
                print(f"[X] 事件失败：{event_name}")

            # 短暂休息
            if event_num < max_events - 1:
                print("[等] 等待3秒...")
                time.sleep(3)

        # 卷完成
        logger.info(f"第{volume}卷写作完成")
        print(f"\n{'='*60}")
        print(f"[卷] 第{volume}卷写作完成！")
        print(f"[析] 最终进度：{self.notes.get_summary()}")
        print(f"{'='*60}")

    def _load_existing_summaries(self):
        """加载已有章节的摘要"""
        logger.info("加载已有章节摘要...")
        chapter_files = sorted(CHAPTERS_DIR.glob("ch*.md"))
        for ch_file in chapter_files:
            try:
                content = ch_file.read_text(encoding='utf-8')
                # 简单摘要：取前100字
                summary = content[:100].replace('\n', ' ')
                self.chapter_summaries.append(f"{ch_file.name}: {summary}")
            except Exception:
                pass
        logger.info(f"加载了{len(self.chapter_summaries)}个章节摘要")

    def _write_parallel(self, ctx: ChapterContext, version: int) -> List[WritingVersion]:
        """并行调用5个写作Agent（5个不同风格的作者）"""
        versions = []
        lock = threading.Lock()

        def write_with_agent(agent, name):
            try:
                result = agent.write(ctx, version)
                if result.content and len(result.content) > 200:
                    with lock:
                        versions.append(result)
                    logger.info(f"{name}生成成功：{result.word_count}字")
                    print(f"  [OK] {name}：{result.word_count}字")
                else:
                    logger.warning(f"{name}生成失败")
                    print(f"  [FAIL] {name}：生成失败")
            except Exception as e:
                logger.error(f"{name}异常：{str(e)[:50]}")
                print(f"  [FAIL] {name}：{str(e)[:50]}")

        # 5个写作Agent并行，每个都是独立的作者
        agents = [
            (self.writer_a, "细雨（细腻派）"),
            (self.writer_b, "烈火（冲突派）"),
            (self.writer_c, "清风（对话派）"),
            (self.writer_d, "磐石（细节派）"),
            (self.writer_e, "迷雾（悬念派）"),
        ]

        print(f"  [笔] 5位作者并行创作...")
        threads = [threading.Thread(target=write_with_agent, args=(agent, name)) for agent, name in agents]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        logger.info(f"生成{len(versions)}个版本")
        return versions

    def _get_previous_text(self) -> str:
        """获取前文内容"""
        chapter_files = sorted(CHAPTERS_DIR.glob("ch*.md"))
        if chapter_files:
            last_chapter = chapter_files[-1]
            return last_chapter.read_text(encoding='utf-8')
        return ""

    def _get_character_states(self) -> Dict[str, str]:
        """获取角色状态（从笔记系统和tracking.md读取）"""
        # 从笔记中读取，如果没有则解析tracking.md
        if self.notes.character_states:
            return self.notes.character_states

        # 尝试从tracking.md解析
        states = self._parse_tracking_characters()
        if states:
            return states

        # 返回空字典（这是通用系统，不应该有硬编码的默认值）
        return {}

    def _get_plot_threads(self) -> List[str]:
        """获取活跃伏笔（从笔记系统和tracking.md读取）"""
        # 从笔记中读取，如果没有则解析tracking.md
        if self.notes.active_plot_threads:
            return self.notes.active_plot_threads

        # 尝试从tracking.md解析
        threads = self._parse_tracking_plot_threads()
        if threads:
            return threads

        # 返回空列表（这是通用系统，不应该有硬编码的默认值）
        return []

    def _parse_tracking_characters(self) -> Dict[str, str]:
        """从tracking.md解析角色状态"""
        tracking_file = PROJECT_DIR / "tracking.md"
        if not tracking_file.exists():
            return {}

        try:
            content = tracking_file.read_text(encoding='utf-8')
            states = {}

            # 查找人物状态表格
            in_character_section = False
            for line in content.split('\n'):
                if '人物状态' in line:
                    in_character_section = True
                    continue
                if in_character_section:
                    # 解析表格行：| 人物 | 状态 | 最新出现 |
                    if line.startswith('|') and '---' not in line:
                        parts = [p.strip() for p in line.split('|') if p.strip()]
                        if len(parts) >= 2 and parts[0] not in ['人物', '状态']:
                            name = parts[0]
                            status = parts[1]
                            states[name] = status
                    elif line.startswith('#') or line.startswith('##'):
                        break  # 进入新的section

            return states
        except Exception as e:
            logger.warning(f"解析tracking.md失败：{e}")
            return {}

    def _parse_tracking_plot_threads(self) -> List[str]:
        """从tracking.md解析伏笔"""
        tracking_file = PROJECT_DIR / "tracking.md"
        if not tracking_file.exists():
            return []

        try:
            content = tracking_file.read_text(encoding='utf-8')
            threads = []

            # 查找伏笔记录表格
            in_plot_section = False
            for line in content.split('\n'):
                if '伏笔记录' in line:
                    in_plot_section = True
                    continue
                if in_plot_section:
                    # 解析表格行：| 伏笔 | 埋设位置 | 状态 | 预计回收 |
                    if line.startswith('|') and '---' not in line:
                        parts = [p.strip() for p in line.split('|') if p.strip()]
                        if len(parts) >= 3 and parts[0] not in ['伏笔', '状态']:
                            plot = parts[0]
                            status = parts[2] if len(parts) > 2 else "活跃"
                            if status == "活跃":
                                threads.append(plot)
                    elif line.startswith('#') or line.startswith('##'):
                        break  # 进入新的section

            return threads
        except Exception as e:
            logger.warning(f"解析tracking.md失败：{e}")
            return []

    def _update_tracking(self, analysis: Dict, event_name: str, event_summary: str = "", chapter_files: List[str] = None):
        """更新追踪文件"""
        if not analysis:
            return

        # 记录伏笔
        plot_threads = analysis.get('plot_threads', [])
        if plot_threads:
            print(f"  [记] 记录：{len(plot_threads)}个伏笔")

        # 记录角色状态变化
        characters = analysis.get('characters', {})
        if characters:
            for char, state in characters.items():
                self.notes.update_character_state(char, state)

        # 记录情节进展
        plot_progress = analysis.get('plot_progress', '')
        if plot_progress:
            self.notes.writing_decisions.append(f"[{event_name}] {plot_progress[:100]}")

        # 使用助手Agent更新tracking.md
        try:
            self.assistant.update_tracking(
                event_name=event_name,
                event_summary=event_summary,
                chapter_files=chapter_files or [],
                analysis=analysis
            )
        except Exception as e:
            logger.warning(f"更新tracking.md失败：{e}")

    def _save_chapter(self, text: str, event_name: str) -> Tuple[str, int]:
        """
        保存章节（一个事件 = 一章，不拆分）

        Returns:
            (章节文件名, 实际字数)
        """
        # 保存原始版本
        raw_file = RAW_DIR / f"event_{event_name}.txt"
        raw_file.write_text(text, encoding='utf-8')
        print(f"  [存] 原始版本：{raw_file}")

        # 保存为一章（不拆分）
        self.chapter_count += 1
        filename = f"ch{self.chapter_count:03d}_{event_name}.md"
        ch_file = CHAPTERS_DIR / filename
        ch_file.write_text(text, encoding='utf-8')

        # 计算实际字数（去除空白字符）
        actual_chars = len(text.replace('\n', '').replace(' ', '').replace('\r', ''))
        print(f"  [书] {filename}（{actual_chars}字）")

        return filename, actual_chars


# ==================== 总导演Agent ====================

class MasterDirectorAgent:
    """
    总导演Agent：规划主线，动态决策

    职责：
    1. 从大纲中读取事件序列
    2. 根据当前进度选择下一个事件
    3. 监控整体进度，确保故事连贯
    4. 把控一卷的整体节奏
    """

    SYSTEM_PROMPT = """你是AI网文写作系统的总导演AI，负责规划故事主线和动态决策。

你的职责：
1. 根据故事规划和当前进度，决定下一个要写的事件
2. 监控整体进度，确保故事连贯
3. 根据已写内容调整后续事件
4. 把控一卷的整体节奏

当前卷：{current_volume}
当前进度：{current_progress}

故事规划参考：
{story_plan}

已写章节摘要：
{chapter_summaries}

请根据以上信息，决定下一个要写的事件。"""

    def __init__(self, story_context: StoryContext):
        self.story_context = story_context
        # 缓存解析后的事件列表
        self._parsed_events: Optional[List[Dict]] = None

    def _parse_events_from_plan(self) -> List[Dict]:
        """从story_plan.md解析事件列表"""
        if self._parsed_events is not None:
            return self._parsed_events

        if not self.story_context.story_plan:
            self._parsed_events = []
            return []

        events = []
        lines = self.story_context.story_plan.split('\n')

        # 解析"第一卷详细规划"部分
        in_volume1 = False
        current_part = ""

        for line in lines:
            # 检测卷开始
            if '第一卷' in line and '详细规划' in line:
                in_volume1 = True
                continue

            # 检测卷结束（遇到其他卷）
            if in_volume1 and ('第二卷' in line or '第三卷' in line or '第四卷' in line or '第五卷' in line):
                break

            if in_volume1:
                # 检测部分标题
                if line.startswith('### 第') and '部分' in line:
                    current_part = line.strip('#').strip()
                    continue

                # 解析事件行：格式 "1. 事件名 - 描述"
                match = re.match(r'^\d+\.\s+(.+?)(?:\s*-\s*(.+))?$', line.strip())
                if match:
                    event_name = match.group(1).strip()
                    event_summary = match.group(2) if match.group(2) else event_name
                    events.append({
                        "name": event_name,
                        "summary": event_summary,
                        "part": current_part
                    })

        self._parsed_events = events
        logger.info(f"从大纲解析了{len(events)}个事件")
        return events

    def decide_next_event(self, current_volume: int, chapter_summaries: List[str]) -> Tuple[str, str]:
        """
        决定下一个要写的事件

        优先从大纲读取，如果没有则让大模型决定

        Args:
            current_volume: 当前卷号
            chapter_summaries: 已写章节的摘要列表

        Returns:
            (事件名称, 事件概述)
        """
        logger.info(f"总导演Agent正在规划下一个事件（当前卷：{current_volume}，已写{len(chapter_summaries)}章）")

        # 从大纲解析事件列表
        events = self._parse_events_from_plan()

        # 计算当前应该写第几个事件（基于已写章节数）
        event_index = len(chapter_summaries)

        # 如果大纲中有对应的事件，直接返回
        if events and event_index < len(events):
            event = events[event_index]
            logger.info(f"从大纲选择事件：{event['name']}（{event['part']}）")
            return event['name'], event['summary']

        # 如果大纲中没有足够的事件，或者没有大纲，让大模型决定
        logger.info("大纲中没有对应事件，使用大模型规划")
        return self._decide_with_llm(current_volume, chapter_summaries)

    def _decide_with_llm(self, current_volume: int, chapter_summaries: List[str]) -> Tuple[str, str]:
        """使用大模型决定下一个事件"""
        # 构建上下文
        current_progress = f"第{current_volume}卷，已写{len(chapter_summaries)}章"
        story_plan = self.story_context.story_plan[:2000] if self.story_context.story_plan else "无"
        summaries_text = "\n".join([f"- {s}" for s in chapter_summaries[-10:]]) if chapter_summaries else "无"

        system_prompt = self.SYSTEM_PROMPT.format(
            current_volume=current_volume,
            current_progress=current_progress,
            story_plan=story_plan,
            chapter_summaries=summaries_text
        )

        user_prompt = """请决定下一个要写的事件。

输出格式：
{
  "event_name": "事件名称",
  "event_summary": "事件概述（100-200字）",
  "reason": "选择这个事件的原因"
}"""

        result = call_api(system_prompt, user_prompt, 500, temperature=0.5)

        # 解析结果
        try:
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                event_name = data.get('event_name', '日常')
                event_summary = data.get('event_summary', event_name)
                reason = data.get('reason', '')
                logger.info(f"大模型决定：{event_name}（原因：{reason[:50]}...）")
                return event_name, event_summary
        except Exception as e:
            logger.warning(f"大模型解析失败：{e}")

        # 最后的兜底
        return "日常", "修行学院的日常生活"

    def summarize_chapter(self, chapter_text: str, event_name: str) -> str:
        """生成章节摘要"""
        system_prompt = """你是AI网文写作系统的摘要AI。请用50-100字概括章节内容，包括：
1. 主要事件
2. 出现的角色
3. 关键情节点"""

        user_prompt = f"""请概括以下章节：

事件：{event_name}

内容：
{chapter_text[:2000]}"""

        result = call_api(system_prompt, user_prompt, 200, temperature=0.3)
        return result if result else f"{event_name}：（摘要生成失败）"

    def get_volume_plan(self, volume: int) -> List[Tuple[str, str]]:
        """
        获取某一卷的事件规划

        Args:
            volume: 卷号

        Returns:
            [(事件名称, 事件概述), ...]
        """
        logger.info(f"获取第{volume}卷的事件规划")

        system_prompt = f"""你是AI网文写作系统的规划AI。请为第{volume}卷规划事件列表。

故事规划参考：
{self.story_context.story_plan[:1500] if self.story_context.story_plan else '无'}

请规划8-12个核心事件，每个事件100-200字概述。"""

        user_prompt = """输出格式：
[
  {"event_name": "事件1", "event_summary": "概述"},
  {"event_name": "事件2", "event_summary": "概述"}
]"""

        result = call_api(system_prompt, user_prompt, 2000, temperature=0.5)

        # 解析结果
        try:
            json_match = re.search(r'\[.*\]', result, re.DOTALL)
            if json_match:
                events = json.loads(json_match.group())
                plan = [(e['event_name'], e['event_summary']) for e in events]
                logger.info(f"第{volume}卷规划了{len(plan)}个事件")
                return plan
        except Exception as e:
            logger.warning(f"卷规划解析失败：{e}")

        # 返回空列表（让总导演自主决定）
        return []


# ==================== 助手Agent ====================

class AssistantAgent:
    """
    助手Agent：管理tracking.md和预处理

    职责：
    1. 更新tracking.md文件
    2. 预处理写作任务，提取关键信息
    3. 更新characters.md, story_plan.md, outline/master.md
    """

    SYSTEM_PROMPT = """你是AI网文写作系统的助手AI，负责管理和更新项目文件。

你的职责：
1. 根据写作内容更新tracking.md
2. 预处理写作任务，提取关键信息
3. 维护项目文件的一致性

当前项目状态：
{project_state}"""

    def __init__(self, story_context: StoryContext):
        self.story_context = story_context

    def update_tracking(self, event_name: str, event_summary: str, chapter_files: List[str], analysis: Dict):
        """更新tracking.md文件"""
        logger.info(f"助手Agent更新tracking.md：{event_name}")
        logger.info(f"事件概述：{event_summary[:100]}...")

        tracking_file = PROJECT_DIR / "tracking.md"

        # 读取现有内容或创建新文件
        if tracking_file.exists():
            content = tracking_file.read_text(encoding='utf-8')
        else:
            content = self._create_tracking_template()

        # 解析现有内容
        sections = self._parse_tracking_sections(content)

        # 更新各个部分
        sections = self._update_project_status(sections, event_name, chapter_files)
        sections = self._update_character_status(sections, analysis.get('characters', {}))
        sections = self._update_event_record(sections, event_name, chapter_files)
        sections = self._update_plot_threads(sections, analysis.get('plot_threads', []))

        # 写入文件
        new_content = self._build_tracking_content(sections)
        tracking_file.write_text(new_content, encoding='utf-8')
        logger.info("tracking.md更新完成")

    def _create_tracking_template(self) -> str:
        """创建tracking.md模板"""
        return """# 追踪文件

## 项目状态

- **当前阶段**：待定
- **已完成**：无
- **总字数**：0

## 人物状态

| 人物 | 状态 | 最新出现 |
|------|------|----------|

## 事件记录

| 编号 | 事件 | 章节 | 状态 |
|------|------|------|------|

## 伏笔记录

| 伏笔 | 埋设位置 | 状态 | 预计回收 |
|------|----------|------|----------|

## 下一步

- 待定
"""

    def _parse_tracking_sections(self, content: str) -> Dict[str, str]:
        """解析tracking.md的各个部分"""
        sections = {}
        current_section = "header"
        current_content = []

        for line in content.split('\n'):
            if line.startswith('## '):
                sections[current_section] = '\n'.join(current_content)
                current_section = line[3:].strip()
                current_content = [line]
            else:
                current_content.append(line)

        sections[current_section] = '\n'.join(current_content)
        return sections

    def _update_project_status(self, sections: Dict[str, str], event_name: str, chapter_files: List[str]) -> Dict[str, str]:
        """更新项目状态"""
        if '项目状态' not in sections:
            sections['项目状态'] = "## 项目状态\n"

        # 更新已完成事件
        status = sections['项目状态']
        chapters_str = ', '.join(chapter_files) if chapter_files else '-'
        if '已完成' in status:
            # 追加新事件
            status = status.replace('已完成', f'已完成：{event_name}（{chapters_str}）')
        sections['项目状态'] = status

        return sections

    def _update_character_status(self, sections: Dict[str, str], characters: Dict[str, str]) -> Dict[str, str]:
        """更新人物状态"""
        if not characters:
            return sections

        if '人物状态' not in sections:
            sections['人物状态'] = "## 人物状态\n\n| 人物 | 状态 | 最新出现 |\n|------|------|----------|\n"

        # 添加新角色
        status = sections['人物状态']
        for char, state in characters.items():
            if char not in status:
                status += f"| {char} | {state} | - |\n"

        sections['人物状态'] = status
        return sections

    def _update_event_record(self, sections: Dict[str, str], event_name: str, chapter_files: List[str]) -> Dict[str, str]:
        """更新事件记录"""
        if '事件记录' not in sections:
            sections['事件记录'] = "## 事件记录\n\n| 编号 | 事件 | 章节 | 状态 |\n|------|------|------|------|\n"

        # 计算事件编号
        events = sections['事件记录']
        event_count = events.count('|') // 4 - 1  # 减去表头
        new_event_num = max(1, event_count + 1)

        # 添加新事件
        chapters_str = ', '.join(chapter_files) if chapter_files else '-'
        sections['事件记录'] += f"| {new_event_num} | {event_name} | {chapters_str} | [OK] |\n"

        return sections

    def _update_plot_threads(self, sections: Dict[str, str], plot_threads: List[str]) -> Dict[str, str]:
        """更新伏笔记录"""
        if not plot_threads:
            return sections

        if '伏笔记录' not in sections:
            sections['伏笔记录'] = "## 伏笔记录\n\n| 伏笔 | 埋设位置 | 状态 | 预计回收 |\n|------|----------|------|----------|\n"

        # 添加新伏笔
        threads = sections['伏笔记录']
        for thread in plot_threads:
            if thread not in threads:
                threads += f"| {thread} | - | 活跃 | 后续章节 |\n"

        sections['伏笔记录'] = threads
        return sections

    def _build_tracking_content(self, sections: Dict[str, str]) -> str:
        """构建tracking.md内容"""
        parts = []
        for section_name in ['header', '项目状态', '人物状态', '事件记录', '伏笔记录', '下一步']:
            if section_name in sections:
                parts.append(sections[section_name])

        return '\n'.join(parts)

    def preprocess_event(self, event_name: str, event_summary: str) -> Dict:
        """预处理写作任务，提取关键信息"""
        logger.info(f"助手Agent预处理事件：{event_name}")

        system_prompt = """你是AI网文写作系统的预处理AI。请分析以下写作任务，提取关键信息。

请输出JSON格式：
{
  "characters": ["相关角色1", "角色2"],
  "key_elements": ["关键元素1", "元素2"],
  "story_stage": "opening/rising/climax/daily",
  "suggested_length": 15000,
  "notes": "注意事项"
}"""

        user_prompt = f"""事件名称：{event_name}
事件概述：{event_summary}

请分析这个写作任务。"""

        result = call_api(system_prompt, user_prompt, 500, temperature=0.3)

        try:
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception as e:
            logger.warning(f"预处理解析失败：{e}")

        return {
            "characters": [],
            "key_elements": [],
            "story_stage": "daily",
            "suggested_length": 15000,
            "notes": ""
        }

    def update_project_files(self, event_name: str, analysis: Dict):
        """更新项目文件（characters.md, story_plan.md等）"""
        logger.info(f"助手Agent更新项目文件：{event_name}")

        # 更新characters.md
        if 'characters' in analysis and analysis['characters']:
            self._update_characters_file(analysis['characters'])

        # 更新story_plan.md
        if 'plot_progress' in analysis:
            self._update_story_plan_file(event_name, analysis['plot_progress'])

    def _update_characters_file(self, characters: Dict[str, str]):
        """更新characters.md"""
        characters_file = PROJECT_DIR / "characters.md"
        if not characters_file.exists():
            return

        content = characters_file.read_text(encoding='utf-8')

        # 检查是否需要添加新角色
        for char, _ in characters.items():
            if char not in content:
                logger.info(f"发现新角色：{char}")
                # 可以选择自动添加或提示用户

    def _update_story_plan_file(self, event_name: str, plot_progress: str):
        """更新story_plan.md"""
        story_plan_file = PROJECT_DIR / "story_plan.md"
        if not story_plan_file.exists():
            return

        # 记录情节进展到笔记中，不直接修改story_plan.md
        logger.info(f"[{event_name}] 情节进展：{plot_progress[:100]}")


# ==================== 故事框架生成 ====================

def generate_story_framework(user_idea: str) -> dict:
    """
    根据用户输入的创意/大纲，AI 生成完整的故事框架

    Args:
        user_idea: 用户输入的故事创意或大纲

    Returns:
        dict: {
            "story_plan": str,      # 故事大纲 (story_plan.md)
            "characters": str,      # 角色设定 (characters.md)
            "events_config": str,   # 事件配置 JSON
            "tracking": str,        # 追踪文件 (tracking.md)
        }
    """
    gui_print("[AI] 正在根据您的创意生成故事框架...")

    # 1. 生成故事大纲
    gui_print("  [1/4] 生成故事大纲...")
    story_plan = call_api(
        system_prompt="""你是一位资深网文策划编辑，擅长将创意扩展为完整的故事大纲。

请根据用户的创意，生成一份详细的故事大纲，格式如下：
- 故事简介（200字以内）
- 世界观设定
- 主线剧情
- 分卷规划（每卷列出5-10个关键事件）
- 伏笔和悬念设计

要求：
- 适合网络连载的节奏
- 每卷有明确的高潮点
- 角色成长线清晰
- 设定自洽，逻辑通顺""",
        user_prompt=f"请根据以下创意生成故事大纲：\n\n{user_idea}",
        max_tokens=8000,
        temperature=0.7
    )

    if not story_plan:
        gui_print("  [X] 故事大纲生成失败")
        return {}

    # 2. 生成角色设定
    gui_print("  [2/4] 生成角色设定...")
    characters = call_api(
        system_prompt="""你是一位资深网文角色设计师。

请根据故事大纲，生成详细的角色设定文档，每个角色包含：
- 姓名和称号
- 外貌描写
- 性格特点
- 背景故事
- 能力/修为
- 与其他角色的关系
- 角色弧线（成长方向）

至少设计5-8个重要角色，包括主角、对手、导师、同伴等。""",
        user_prompt=f"故事大纲：\n\n{story_plan}\n\n请生成角色设定。",
        max_tokens=6000,
        temperature=0.7
    )

    # 3. 生成第一卷事件列表
    gui_print("  [3/4] 生成第一卷事件列表...")
    events_json_str = call_api(
        system_prompt="""你是一位网文写作系统的事件规划师。

请根据故事大纲和角色设定，为第一卷生成事件列表。

输出格式必须是合法的 JSON 数组，每个事件包含：
{
  "events": [
    {
      "name": "事件名称",
      "summary": "事件简要描述（50-100字）",
      "characters": ["参与角色1", "参与角色2"],
      "scene": "场景描述",
      "status": "pending"
    }
  ]
}

生成5-10个事件，覆盖第一卷的主要情节。""",
        user_prompt=f"故事大纲：\n\n{story_plan}\n\n角色设定：\n\n{characters}\n\n请生成第一卷的事件列表。",
        max_tokens=4000,
        temperature=0.7
    )

    # 尝试解析 events JSON
    events_config = events_json_str
    try:
        # 提取 JSON 部分
        json_match = re.search(r'\{.*\}', events_json_str, re.DOTALL)
        if json_match:
            events_config = json_match.group()
            # 验证是合法 JSON
            json.loads(events_config)
    except Exception:
        gui_print("  [WARN] 事件列表格式可能不正确，将使用原始输出")

    # 4. 生成追踪文件
    gui_print("  [4/4] 初始化追踪文件...")
    tracking = f"""# 创作追踪

## 角色状态
（等待开始创作后自动更新）

## 事件记录
（等待开始创作后自动更新）

## 活跃伏笔
（等待开始创作后自动更新）
"""

    gui_print("[OK] 故事框架生成完成！")

    return {
        "story_plan": story_plan,
        "characters": characters,
        "events_config": events_config,
        "tracking": tracking,
    }


def save_story_framework(framework: dict, project_dir: Path = None):
    """将故事框架保存到项目文件"""
    if project_dir is None:
        project_dir = PROJECT_DIR

    if framework.get("story_plan"):
        (project_dir / "story_plan.md").write_text(framework["story_plan"], encoding="utf-8")
    if framework.get("characters"):
        (project_dir / "characters.md").write_text(framework["characters"], encoding="utf-8")
    if framework.get("events_config"):
        try:
            events = json.loads(framework["events_config"])
            (project_dir / "events_config.json").write_text(
                json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except json.JSONDecodeError:
            (project_dir / "events_config.json").write_text(framework["events_config"], encoding="utf-8")
    if framework.get("tracking"):
        (project_dir / "tracking.md").write_text(framework["tracking"], encoding="utf-8")

    gui_print(f"[OK] 故事框架已保存到 {project_dir}")


# ==================== 命令行入口 ====================

def _run_cli():
    """命令行模式入口"""
    import sys

    system = NovelWritingSystem()

    if len(sys.argv) < 2 or sys.argv[1] == "--help":
        print("""
AI 网文写作系统 — 命令行模式

用法：
    python agents.py --cli <事件名称> [事件概述]
    python agents.py --cli --volume <卷号> [--max-events <数量>]
    python agents.py --cli --status
    python agents.py --cli --reset
    python agents.py --cli --config [show|set <key> <value>]

启动 GUI：
    python agents.py
    python agents.py --gui
""")
        return

    cmd = sys.argv[1]

    if cmd == "--status":
        gui_print(f"\n[析] 当前创作状态：")
        gui_print(f"{'='*60}")
        gui_print(system.notes.get_summary())
        gui_print(f"{'='*60}")

    elif cmd == "--reset":
        if gui_confirm("确定要重置所有进度吗？"):
            system.notes = WritingNotes()
            system.notes.save()
            gui_print("[OK] 进度已重置")

    elif cmd == "--config":
        if len(sys.argv) < 3 or sys.argv[2] == "show":
            gui_print(f"\n[配] 当前配置：")
            gui_print(f"{'='*60}")
            gui_print(json.dumps(CONFIG, ensure_ascii=False, indent=2))
            gui_print(f"{'='*60}")
        elif sys.argv[2] == "set" and len(sys.argv) >= 5:
            key_path = sys.argv[3]
            value = sys.argv[4]
            keys = key_path.split('.')
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    if value.lower() == 'true':
                        value = True
                    elif value.lower() == 'false':
                        value = False
            config = CONFIG
            for key in keys[:-1]:
                if key not in config:
                    config[key] = {}
                config = config[key]
            config[keys[-1]] = value
            save_config(CONFIG)
            gui_print(f"[OK] 配置已更新：{key_path} = {value}")
        else:
            gui_print("[X] 用法：python agents.py --cli --config set <key> <value>")

    elif cmd == "--volume":
        volume = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        max_events = get_writing_config().get("max_events_per_volume", 15)
        for i, arg in enumerate(sys.argv):
            if arg == "--max-events" and i + 1 < len(sys.argv):
                max_events = int(sys.argv[i + 1])
        system.run_volume(volume, max_events)

    else:
        event_name = cmd
        event_summary = sys.argv[2] if len(sys.argv) > 2 else event_name
        system.run_event(event_name, event_summary)


def main():
    """入口：默认启动 GUI，--cli 进入命令行模式"""
    import sys

    # 启动 GUI 模式
    if len(sys.argv) < 2 or sys.argv[1] in ("--gui",):
        try:
            from gui import launch_gui
            launch_gui()
        except ImportError as e:
            print(f"[X] 无法启动 GUI：{e}")
            print("请确保 gui.py 在同一目录下")
            sys.exit(1)
        except Exception as e:
            print(f"[X] GUI 启动失败：{e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
        return

    # 命令行模式
    if sys.argv[1] == "--cli":
        sys.argv = [sys.argv[0]] + sys.argv[2:]  # 移除 --cli 参数
        _run_cli()
        return

    # 兼容旧用法：直接运行不带 --cli 也进入命令行
    _run_cli()


if __name__ == "__main__":
    main()
