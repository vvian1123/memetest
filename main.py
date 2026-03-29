import subprocess
import sys
import importlib

DEPENDENCIES = ["jieba", "lunar_python", "Pillow","aiohttp"]

def install_dependencies():
    for pkg in DEPENDENCIES:
        import_name = pkg.replace("-", "_")
        try:
            importlib.import_module(import_name)
        except ImportError:
            print(f"检测到缺少依赖 {pkg}，正在尝试自动安装...", flush=True)
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
                print(f"依赖 {pkg} 安装成功！", flush=True)
            except Exception as e:
                print(f"依赖 {pkg} 安装失败: {e}", flush=True)

install_dependencies()

import jieba
import jieba.analyse
import os
import json
import random
import asyncio
import time
import re
import aiohttp
import difflib
import zipfile
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders 
import sqlite3
import io
import datetime
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from PIL import Image as PILImage

try:
    from lunar_python import Solar
    HAS_LUNAR = True
except ImportError:
    HAS_LUNAR = False

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.components import Image, Plain

print(">>> [Meme] 插件主文件 v23 (Logic Perfected) 已被系统加载 <<<", flush=True)

@register("vv_meme_master", "Vvivloy", "防抖/图库/记忆", "3.0.0")
class MemeMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.img_dir = os.path.join(self.base_dir, "images")
        self.data_file = os.path.join(self.base_dir, "memes.json")
        self.config_file = os.path.join(self.base_dir, "config.json")
        self.memory_file = os.path.join(self.base_dir, "memory.txt") 
        self.buffer_file = os.path.join(self.base_dir, "buffer.json") 
        self.init_db()

        self.db_lock = asyncio.Lock()          # 数据库写入锁 (防 locked)
        self.api_semaphore = asyncio.Semaphore(1) # 鉴图并发锁 (防 429)
        
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.running = True
        
        if not os.path.exists(self.img_dir): os.makedirs(self.img_dir, exist_ok=True)
            
        self.local_config = self.load_config()
        if "web_token" not in self.local_config:
            self.local_config["web_token"] = "admin123"
            self.save_config()

        self.data = self.load_data()
        self.chat_history_buffer = self.load_buffer_from_disk()
        self.current_summary = self.load_memory()
        self.img_hashes = {} 
        self.sessions = {} 
        # round_count 和 sticky_updated 从 DB 读取，不再用实例变量
        self.pending_user_msg = ""
        self.rounds_since_sticky = 0
        self.config_mtime = os.path.getmtime(self.config_file) if os.path.exists(self.config_file) else 0

        
        self.is_summarizing = False
        self.last_auto_save_time = 0
        self.last_active_time = time.time()
        self.last_email_date = ""

        self.pair_map = {'“': '”', '《': '》', '（': '）', '(': ')', '"': '"', "'": "'"}
        self.split_chars = "\n。？！?!"

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.start_web_server())
            loop.create_task(self._init_image_hashes())
            loop.create_task(self._lonely_watcher())
            print("✅ [Meme] 核心服务启动成功！", flush=True)
        except Exception as e:
            print(f"❌ [Meme] 服务启动失败: {e}", flush=True)

    def init_db(self):
        """初始化 SQLite 数据库 (v2.0)"""
        db_path = os.path.join(self.base_dir, "meme_core.db")
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # 1. 表情表 (增加 tags 索引以加速检索)
        c.execute('''CREATE TABLE IF NOT EXISTS memes (
            filename TEXT PRIMARY KEY,
            tags TEXT,
            feature_hash TEXT,
            source TEXT DEFAULT 'manual',
            created_at REAL,
            last_used REAL DEFAULT 0,
            usage_count INTEGER DEFAULT 0
        )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_memes_tags ON memes(tags);")
        
        # 2. 记忆表 (核心大改：增加 keywords 字段)
        c.execute('''CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            keywords TEXT,      -- 存 jieba 提取的关键词
            type TEXT DEFAULT 'dialogue', -- dialogue(流水), sticky(重要), fragment(旧摘要)
            importance INTEGER DEFAULT 1,
            created_at REAL
        )''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_keywords ON memories(keywords);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);")
        
        # 3. 访问日志 & 配置表 (保持原样)
        c.execute('''CREATE TABLE IF NOT EXISTS access_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, action_type TEXT, timestamp REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT)''')

        conn.commit()
        conn.close()
        print("✅ [Meme] 数据库 v2.0 初始化完成 (索引已建立)", flush=True)

    def _get_config_val(self, key, default="0"):
        """从 system_config 表读取状态值"""
        try:
            conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"), timeout=5)
            c = conn.cursor()
            c.execute("SELECT value FROM system_config WHERE key=?", (key,))
            row = c.fetchone()
            conn.close()
            return row[0] if row else default
        except:
            return default

    def _set_config_val(self, key, value):
        """写入 system_config 表"""
        try:
            conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"), timeout=5)
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ [Config] 写入失败 key={key}: {e}", flush=True)


    def extract_keywords(self, text):
        """本地离线分词 (0成本)"""
        if not text: return ""
        # 提取前10个关键词，允许名词(n)、动词(v)、人名(nr)等
        tags = jieba.analyse.extract_tags(text, topK=10, allowPOS=('n', 'nr', 'ns', 'nt', 'nz', 'v', 'vn'))
        return ",".join(tags)

    async def save_message_to_db(self, content, msg_type='dialogue'):
        """v24: 异步锁 + 报错屏蔽"""
        if not content: return
        if "AstrBot 请求失败" in content or "请在平台日志查看" in content:
            return
        async with self.db_lock: # <--- 关键：拿锁
            try:
                kw = self.extract_keywords(content)
                conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"), timeout=10) # 增加 timeout
                c = conn.cursor()
                c.execute("INSERT INTO memories (content, keywords, type, created_at) VALUES (?, ?, ?, ?)",
                          (content, kw, msg_type, time.time()))
                conn.commit()
                conn.close()
            except sqlite3.OperationalError:
                # 屏蔽 database is locked 报错
                pass 
            except Exception as e:
                print(f"❌ 存库小错误: {e}", flush=True)

    def get_related_context(self, current_text):
        """智能检索：找 Sticky + 找相关回忆 (0成本)"""
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        context_list = []
        
        # 1. 必读：Sticky 核心规则 (永远置顶)
        c.execute("SELECT content FROM memories WHERE type='sticky' ORDER BY created_at DESC")
        stickies = [f"【核心设定/重要事实】 {row['content']}" for row in c.fetchall()]
        if stickies: context_list.extend(stickies)

        # 2. 选读：根据当前那句话，去搜相关的旧记忆
        if current_text:
            search_kws = list(jieba.cut_for_search(current_text)) # 比如 "想吃苹果" -> 苹果
            search_kws = [w for w in search_kws if len(w) > 1]    # 过滤单字
            
            if search_kws:
                # 构造 SQL: keywords LIKE '%苹果%' OR content LIKE '%苹果%'
                conditions = []
                params = []
                for w in search_kws:
                    conditions.append("(keywords LIKE ? OR content LIKE ?)")
                    params.extend([f"%{w}%", f"%{w}%"])
                
                if conditions:
                    # 只找 type='dialogue' (旧对话) 或 'fragment' (旧摘要)，限制 3 条，最近的优先
                    sql = f"SELECT content, created_at FROM memories WHERE type IN ('dialogue', 'fragment') AND ({' OR '.join(conditions)}) ORDER BY created_at DESC LIMIT 3"
                    c.execute(sql, tuple(params))
                    related = [f"【相关回忆】 {row['content']}" for row in c.fetchall()]
                    if related: context_list.extend(related)
        
        conn.close()
        return "\n".join(context_list)
        
    def get_meme_candidates(self, current_text):
        """v24: 情绪反转 + 关键词混合检索 (让AI做选择)"""
        if not current_text: return []
        
        # 1. 情绪反转字典 (用户哭 -> 搜安慰)
        mood_map = {
            '负面': {'keywords': ['难过', '哭', '累', '死', '痛', '委屈', '烦', '不开心'], 'search': ['安慰', '抱抱', '摸摸', '贴贴', '爱你']},
            '正面': {'keywords': ['开心', '哈哈', '笑死', '耶', '棒'], 'search': ['震惊', '庆祝', '无语', '疑惑']}
        }
        
        search_terms = set()
        
        # 2. 也是用 jieba 分词
        user_kws = list(jieba.cut(current_text))
        
        # 3. 命中情绪词？强制加入反转词
        hit_mood = False
        for k in user_kws:
            for m_type, m_data in mood_map.items():
                if k in m_data['keywords']:
                    search_terms.update(m_data['search'])
                    hit_mood = True
        
        # 4. 如果没命中强情绪，或者为了多样性，也加上原文关键词
        search_terms.update([w for w in user_kws if len(w) > 1])
        
        if not search_terms: return []

        # 5. 查库
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        candidates = []
        
        try:
            for term in list(search_terms):
                # 模糊匹配标签
                c.execute("SELECT tags FROM memes WHERE tags LIKE ? ORDER BY usage_count DESC LIMIT 3", (f"%{term}%",))
                for row in c.fetchall():
                    candidates.append(row[0])
            # === 保底机制：如果搜出来的太少，随机补货 ===
            if len(candidates) < 2:
                # 随机拿 5 个，保证 AI 只要想发图总有货
                c.execute("SELECT tags FROM memes ORDER BY RANDOM() LIMIT 3")
                for row in c.fetchall():
                    candidates.append(row[0])
        except: pass
        finally: conn.close()
        
        # 6. 随机打散并取前 6 个，给 AI 更多选择空间
        final_list = list(set(candidates))
        random.shuffle(final_list)
        return final_list[:6]
    
    def merge_legacy_data(self, legacy_memes=None, legacy_memory="", legacy_buffer=None):
        """将旧 JSON 数据导入 SQLite"""
        try:
            conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
            c = conn.cursor()
            count = 0
            
            # 1. 导入旧 meme.json
            if legacy_memes:
                for fn, info in legacy_memes.items():
                    try:
                        c.execute("INSERT OR IGNORE INTO memes (filename, tags, source, feature_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                                  (fn, info.get('tags'), info.get('source', 'manual'), info.get('hash', ''), time.time()))
                        count += 1
                    except: pass
            
            # 2. 导入 memory.txt (按段落粒度切割)
            if legacy_memory and legacy_memory.strip():
                current_date = ""
                # 按行遍历，识别日期 + 按空行分段
                paragraphs = []
                current_para = []
                
                for line in legacy_memory.split("\n"):
                    stripped = line.strip()
                    
                    # 识别 C格式日期: --- 2025-12-29 ... ---
                    date_c = re.match(r"---\s*(\d{4}-\d{2}-\d{2}).*---", stripped)
                    # 识别 B格式日期: 2025年12月16日 | 标题
                    date_b = re.match(r"(\d{4}年\d{1,2}月\d{1,2}日)\s*[|｜]", stripped)
                    # 识别编号标题: 1. xxx  2. xxx (A格式大总结)
                    numbered = re.match(r"^\d+\.\s+", stripped)
                    
                    if date_c:
                        # 遇到新日期，先保存之前的段落
                        if current_para:
                            paragraphs.append((current_date, "\n".join(current_para)))
                            current_para = []
                        current_date = date_c.group(1)
                    elif date_b:
                        if current_para:
                            paragraphs.append((current_date, "\n".join(current_para)))
                            current_para = []
                        current_date = date_b.group(1)
                        current_para.append(stripped)  # 标题行也保留
                    elif numbered and len(current_para) > 2:
                        # 编号标题且已有内容，分段
                        paragraphs.append((current_date, "\n".join(current_para)))
                        current_para = [stripped]
                    elif stripped == "":
                        # 空行 = 段落分隔（但要求当前段落至少有内容）
                        if current_para:
                            paragraphs.append((current_date, "\n".join(current_para)))
                            current_para = []
                    else:
                        current_para.append(stripped)
                
                # 别忘了最后一段
                if current_para:
                    paragraphs.append((current_date, "\n".join(current_para)))
                
                # 存入数据库
                for date_str, content in paragraphs:
                    content = content.strip()
                    if not content or len(content) < 5:
                        continue  # 跳过太短的碎片
                    # 有日期就加前缀
                    full_content = f"{date_str} | {content}" if date_str else content
                    kw = self.extract_keywords(full_content)
                    c.execute("INSERT INTO memories (content, keywords, type, created_at) VALUES (?, ?, 'fragment', ?)",
                              (full_content, kw, time.time()))
            # 3. 导入 buffer.json
            if legacy_buffer and isinstance(legacy_buffer, list):
                for msg in legacy_buffer:
                    msg_str = str(msg).strip()
                    if msg_str and "AstrBot 请求失败" not in msg_str:
                        kw = self.extract_keywords(msg_str)
                        c.execute("INSERT INTO memories (content, keywords, type, created_at) VALUES (?, ?, 'dialogue', ?)",
                                  (msg_str, kw, time.time()))

            conn.commit()
            conn.close()
            return True, f"成功导入 {count} 张图片 + 记忆 + 对话记录"
        except Exception as e:
            print(f"❌ [Meme] 数据迁移失败: {e}", flush=True)
            return False, str(e)


    def get_db_context(self, current_query=""):
        """v24: 只提取 Sticky + 强相关记忆 (非最近)"""
        try:
            conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            
            # 1. Sticky: 按冷却策略，这里只取数据，注入逻辑在 handler 里做
            c.execute("SELECT content FROM memories WHERE type='sticky'")
            stickies = [row['content'] for row in c.fetchall()]

            related_fragments = []
            
            # 2. 相关性检索：用 TF-IDF 提取关键词，评分制匹配
            if current_query:
                # 用 extract_tags (TF-IDF) 提取有区分度的关键词
                query_words = [w for w in jieba.analyse.extract_tags(current_query, topK=5) if len(w) > 1]
                if query_words:
                    # 构建评分表达式：每命中一个关键词 +1 分
                    score_parts = []
                    params_score = []
                    for w in query_words:
                        score_parts.append("(CASE WHEN keywords LIKE ? OR content LIKE ? THEN 1 ELSE 0 END)")
                        params_score.extend([f"%{w}%", f"%{w}%"])
                    score_expr = " + ".join(score_parts)
                    
                    # 最低匹配要求：多关键词至少命中2个，单关键词命中1个
                    min_score = 2 if len(query_words) >= 2 else 1
                    
                    context_window = int(self.local_config.get("ab_context_rounds", 50))
                    
                    # 拿 2 条相关总结 (fragment)，按评分排序
                    sql_frag = f"SELECT content, ({score_expr}) as score FROM memories WHERE type='fragment' AND ({score_expr}) >= {min_score} ORDER BY score DESC, created_at DESC LIMIT 2"
                    c.execute(sql_frag, tuple(params_score * 2))
                    related_fragments = [f"【相关总结】{row['content']}" for row in c.fetchall()]
                    
                    # 拿 4 条相关原文 (dialogue)，按时间排序+跳过AB上下文，评分仅做筛选
                    sql_dial = f"SELECT content, ({score_expr}) as score FROM memories WHERE type='dialogue' AND ({score_expr}) >= {min_score} ORDER BY created_at DESC LIMIT 4 OFFSET {context_window}"
                    c.execute(sql_dial, tuple(params_score * 2))
                    related_fragments += [f"【相关对话】{row['content']}" for row in c.fetchall()]

            conn.close()
            
            # 组装
            context_list = []
            # Sticky 不在这里拼装，由主逻辑控制频率
            if related_fragments: 
                context_list.extend(related_fragments)
                
            return stickies, "\n".join(context_list) # 返回元组
        except Exception as e:
            return [], ""
            
    def __del__(self):
        self.running = False 

    async def _debounce_timer(self, uid: str, duration: float):
        """防抖计时器: 支持正在输入延长"""
        try:
            await asyncio.sleep(duration)
            # 智能延长: 仅在启用输入检测时生效
            if self.local_config.get("typing_debounce", "off").strip().lower() in ["on", "1", "true", "开"]:
                typing_times = getattr(self, '_typing_times', {})
                user_id = uid.split(':')[-1] if ':' in uid else uid
                max_extra_wait = 30
                waited = 0
                while waited < max_extra_wait:
                    last_type = typing_times.get(user_id, 0)
                    if time.time() - last_type < 3.0:
                        print(f"⌨️ [Debounce] 用户还在输入，继续等待...", flush=True)
                        await asyncio.sleep(2.0)
                        waited += 2
                    else:
                        break
            if uid in self.sessions:
                self.sessions[uid]['flush_event'].set()
        except asyncio.CancelledError:
            pass

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=1)
    async def handle_private(self, event: AstrMessageEvent):
        await self._master_handler(event)

    # 群聊暂不处理（避免刷量和上下文混乱）
    # @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=1)
    # async def handle_group(self, event: AstrMessageEvent):
    #     await self._master_handler(event)

    # === 正在输入检测: 延长防抖 ===
    @filter.event_message_type(EventMessageType.ALL, priority=99)
    async def notice_handler(self, event: AstrMessageEvent):
        try:
            # 未开启则跳过
            if self.local_config.get("typing_debounce", "off").strip().lower() not in ["on", "1", "true", "开"]:
                return
            raw = getattr(event.message_obj, 'raw_message', None)
            if not isinstance(raw, dict) or raw.get('post_type') != 'notice':
                return
            if raw.get('notice_type') == 'notify' and raw.get('sub_type') == 'input_status':
                uid = str(raw.get('user_id', ''))
                if uid:
                    if not hasattr(self, '_typing_times'):
                        self._typing_times = {}
                    self._typing_times[uid] = time.time()
                    print(f"⌨️ [Typing] 用户 {uid} 正在输入", flush=True)
        except Exception:
            pass

    # ==========================
    # 主逻辑
    # ==========================
    async def _master_handler(self, event: AstrMessageEvent):
        # 1. 基础防爆 & 自检 (保持不变)
        try:
            user_id = str(event.message_obj.sender.user_id)
            if hasattr(self.context, 'get_current_provider_bot'):
                bot = self.context.get_current_provider_bot()
                if bot and user_id == str(bot.self_id): return
                
                # 机器人白名单验证：如果配置了目标bot且当前bot不符，跳过
                target_bot = self.local_config.get("target_bot_id", "").strip()
                if target_bot and bot and str(bot.self_id) != target_bot:
                    return
        except Exception as e:
            print(f"⚠️ [Meme] bot_id 检测失败: {e}", flush=True)

        try:
            self.check_config_reload()
            msg_str = (event.message_str or "").strip()
            uid = event.unified_msg_origin
            img_urls = self._get_all_img_urls(event)

            # 空消息过滤
            if not msg_str and not img_urls: return 

            print(f"📨 [Meme] 收到: {msg_str[:10]}... (图:{len(img_urls)})", flush=True)
            self.last_active_time = time.time()
            self.last_uid = uid
            self.last_session_id = event.session_id

            # 自动进货 (逻辑微调：加冷却判断)
            if img_urls:
                cd = float(self.local_config.get("auto_save_cooldown", 60))
                if time.time() - self.last_auto_save_time > cd:
                    self.last_auto_save_time = time.time()
                    for url in img_urls:
                        # 传入 msg_str 作为上下文！
                        asyncio.create_task(self.ai_evaluate_image(url, msg_str))

            # === /撤回 命令：撤回刚刚输入的一句话 ===
            if msg_str in ["/撤回", "/undo"]:
                has_pending = False
                recalled_content = ""
                if uid in self.sessions and self.sessions[uid]['queue']:
                    has_pending = True
                    # 只移除队列里的最后一条消息
                    last_item = self.sessions[uid]['queue'].pop()
                    recalled_content = last_item.get('content', '[图片]') if last_item.get('type') == 'text' else '[图片]'
                    
                    # 如果撤回后队列空了，说明全撤回了，取消本次 AI 请求
                    if not self.sessions[uid]['queue']:
                        self.sessions[uid]['flush_event'].set()

                setattr(event, "__meme_skipped", True)
                if has_pending:
                    msg = f"✅ 已撤回: {recalled_content}"
                else:
                    msg = "⚠️ 当前没有正在输入的待发消息可以撤回"
                event.set_result(MessageEventResult().message(msg))
                event.stop_event()
                return

            # 指令穿透: 其他命令消息不走我们的处理流程
            cmd_prefixes = ["/", "！", "!"]
            if msg_str and any(msg_str.startswith(p) for p in cmd_prefixes) and not img_urls:
                if uid in self.sessions: self.sessions[uid]['flush_event'].set()
                setattr(event, "__meme_skipped", True)
                return 
            
            # "/" 开头兜底
            if msg_str.startswith("/"):
                setattr(event, "__meme_skipped", True)
                return
            try: debounce_time = float(self.local_config.get("debounce_time", 3.0))
            except: debounce_time = 3.0
            print(f"🔧 [Meme] 防抖值: {debounce_time}", flush=True)  # ← 加这行


            if debounce_time > 0:
                if uid in self.sessions:
                    s = self.sessions[uid]
                    if msg_str: s['queue'].append({'type': 'text', 'content': msg_str})
                    for url in img_urls: s['queue'].append({'type': 'image', 'url': url})
                    
                    if s.get('timer_task'): s['timer_task'].cancel()
                    s['timer_task'] = asyncio.create_task(self._debounce_timer(uid, debounce_time))
                    
                    event.stop_event()
                    print(f"⏳ [Meme] 防抖追加 (Q:{len(s['queue'])})", flush=True)
                    return 

                print(f"🆕 [Meme] 启动防抖 ({debounce_time}s)...", flush=True)
                flush_event = asyncio.Event()
                timer_task = asyncio.create_task(self._debounce_timer(uid, debounce_time))
                
                initial_queue = []
                if msg_str: initial_queue.append({'type': 'text', 'content': msg_str})
                for url in img_urls: initial_queue.append({'type': 'image', 'url': url})

                self.sessions[uid] = {
                    'queue': initial_queue, 'flush_event': flush_event, 'timer_task': timer_task
                }
                
                await flush_event.wait()
                
                if uid not in self.sessions: 
                    event.stop_event()
                    return 
                s = self.sessions.pop(uid)
                queue = s['queue']
                if not queue: 
                    event.stop_event()
                    return

                combined_text_list = []
                combined_images = []
                for item in queue:
                    if item['type'] == 'text': combined_text_list.append(item['content'])
                    elif item['type'] == 'image': combined_images.append(item['url'])
                
                msg_str = " ".join(combined_text_list)
                img_urls = combined_images

            # === 核心修改区开始 ===
            
            # 1. 【防应声虫】：绝对不在这里 save_message_to_db (User)！
            # 我们把用户的话暂存在 event 对象里，传给 on_output
            event.user_text_raw = msg_str 
            if img_urls:
                setattr(event, "user_has_image", True)

            # 2. 准备 System Context
            time_info = self.get_full_time_str()
            
            # 3. 检索记忆 (异步执行，防止阻塞)
            stickies, related_context = await asyncio.to_thread(self.get_db_context, msg_str)
            
            # 4. Sticky 注入逻辑（频率由 ab_context_rounds 自动计算）
            ab_rounds = int(self.local_config.get("ab_context_rounds", 50))
            sticky_freq = ab_rounds if ab_rounds <= 20 else ab_rounds // 2
            
            # === Sticky 变更检测 (用内容 hash，不依赖 h_update_sticky 写标记) ===
            round_count = int(self._get_config_val("round_count", "0"))
            current_hash = str(hash(tuple(sorted(stickies)))) if stickies else "empty"
            stored_hash = self._get_config_val("sticky_hash", "")
            sticky_changed = (current_hash != stored_hash) and stickies
            should_inject = (round_count % sticky_freq == 0) or sticky_changed
            
            print(f"🔍 [Sticky Debug] round_count={round_count}, sticky_freq={sticky_freq}, "
                  f"stickies数量={len(stickies)}, 条件={should_inject}, "
                  f"内容变更={sticky_changed}, "
                  f"内容={stickies[:2] if stickies else '空'}", flush=True)
            
            # 5. 组装 system_tag (顺序: Sticky → 回忆 → 表情包)
            system_tag = "<system_context>\n"
            
            if should_inject and stickies:
                sticky_str = " ".join([f"({s})" for s in stickies])
                system_tag += f"Important Facts (Established Knowledge): {sticky_str}\n"
                system_tag += "(NOTE: You already KNOW these facts. Do NOT repeat them in your response unless asked.)\n"
                # 更新 hash + 重置计数
                self._set_config_val("sticky_hash", current_hash)
                if sticky_changed:
                    self._set_config_val("round_count", "0")

            if related_context:
                system_tag += f"Historical Context (Recall): {related_context}\n"
                system_tag += "(NOTE: The above 'Historical Context' is for background info ONLY. Do NOT reply to it as if it marks the current conversation state.)\n"
            
            # 6. 智能检索表情包 (异步执行)
            meme_hints = await asyncio.to_thread(self.get_meme_candidates, msg_str)
            if meme_hints:
                hints_str = " ".join([f"<MEME:{t}>" for t in meme_hints])
                system_tag += f"Available Memes (copy EXACT tag to use): {hints_str}\n"

            system_tag += f"Time: {time_info}\n"
            system_tag += "</system_context>"

            # === DEBUG 2: 看最终拼出来的 system_tag ===
            print(f"🔍 [Sticky Debug 2] system_tag前200字: {system_tag[:200]}", flush=True)

            # 7. 构造最终文本 (system_context 在前, 用户消息在后)
            final_text = f"{system_tag}\n\n{msg_str}"
            
            event.message_str = final_text
            chain = [Plain(final_text)]
            for url in img_urls: chain.append(Image.fromURL(url))
            event.message_obj.message = chain
        except Exception as e:
            # 屏蔽空消息日志
            if "not defined" not in str(e): # 过滤常见无关报错
                print(f"❌ [Meme] Handle Error: {e}", flush=True)


    @filter.on_decorating_result(priority=0)
    async def on_output(self, event: AstrMessageEvent):
        if getattr(event, "__meme_processed", False): return
        if getattr(event, "__meme_skipped", False): return  # 命令消息跳过
        
        result = event.get_result()
        if not result: return
        
        text = ""
        if isinstance(result, list):
            for c in result:
                if isinstance(c, Plain): text += c.text
        elif hasattr(result, "chain"):
            for c in result.chain:
                if isinstance(c, Plain): text += c.text
        else: text = str(result)
            
        if not text: return
        setattr(event, "__meme_processed", True)
        
        # === DEBUG 3: 看 on_output 收到的原始文本 ===
        has_meme_tag = "<MEME:" in text or "MEME_TAG:" in text
        print(f"🔍 [Meme Debug] 有标签={has_meme_tag}, 原文前150字: {text[:150]}", flush=True)
        
        text = self.clean_markdown(text)
        
        # 如果 AI 输出了 [[MEM:生日是5.20]]，立刻拦截并存入 Sticky
        mem_match = re.search(r"\[\[MEM:(.*?)\]\]", text)
        if mem_match:
            mem_content = mem_match.group(1).strip()
            if mem_content:
                print(f"📝 [Meme] AI 捕获重要事实: {mem_content}", flush=True)
                # 存为 sticky 类型
                await self.save_message_to_db(mem_content, 'sticky')
                self.sticky_updated = True
            # 从回复给用户的文本里删掉这行指令，用户看不到
            text = text.replace(mem_match.group(0), "").strip()

        # 防应声虫：成对存库
        user_raw = getattr(event, "user_text_raw", "")
        if getattr(event, "user_has_image", False):
            user_raw += " [图片]"
        user_raw = user_raw.strip()
        
        # 清理掉 System Context (兼容旧版和新 XML 版)
        ai_clean = re.sub(r"\(System Context:.*?\)", "", text).strip()
        ai_clean = re.sub(r"<system_context>.*?</system_context>", "", ai_clean, flags=re.DOTALL).strip()
        ai_clean = re.sub(r"<MEME:.*?>", "", ai_clean).strip()  # 也清掉 meme 标签
        
        if user_raw and ai_clean:
            pair_log = f"User: {user_raw}\nAI: {ai_clean}"
            self.chat_history_buffer.append(pair_log)
            self.save_buffer_to_disk()
            await self.save_message_to_db(pair_log, 'dialogue')
            new_rc = int(self._get_config_val("round_count", "0")) + 1
            self._set_config_val("round_count", str(new_rc))
            self.rounds_since_sticky += 1

        
        if not self.is_summarizing:
            asyncio.create_task(self.check_and_summarize())

        try:
            # 表情包解析 (兼容反引号包裹)
            pattern = r"(`?<MEME:.*?>`?|MEME_TAG:\s*[\S]+)"
            parts = re.split(pattern, text)
            mixed_chain = []
            has_meme = False
            
            for part in parts:
                tag = None
                # 兼容 `<MEME:xxx>` 和 <MEME:xxx> 两种格式
                clean_part_meme = part.strip("`")
                if clean_part_meme.startswith("<MEME:"): tag = clean_part_meme[6:-1].strip()
                elif "MEME_TAG:" in part: tag = part.replace("MEME_TAG:", "").strip()
                
                if tag:
                    path = self.find_best_match(tag)
                    if path: 
                        print(f"🎯 [Meme] 命中表情包: [{tag}]", flush=True)
                        mixed_chain.append(Image.fromFileSystem(path))
                        has_meme = True
                elif part:
                    clean_part = re.sub(r"\(System Context:.*?\)", "", part).strip()
                    if clean_part: mixed_chain.append(Plain(clean_part))
            
            if not has_meme and len(text) < 50 and "\n" not in text:
                event.set_result(MessageChain([Plain(text)]))
                return


            segments = self.smart_split(mixed_chain)
            delay_base = self.local_config.get("delay_base", 0.5)
            delay_factor = self.local_config.get("delay_factor", 0.1)
            
            for i, seg in enumerate(segments):
                txt_len = sum(len(c.text) for c in seg if isinstance(c, Plain))
                wait = delay_base + (txt_len * delay_factor)
                
                mc = MessageChain()
                mc.chain = seg
                await self.context.send_message(event.unified_msg_origin, mc)
                if i < len(segments) - 1: await asyncio.sleep(wait)
            
            event.set_result(None) 

        except Exception as e:
            print(f"❌ [Meme] 输出处理出错: {e}", flush=True)
            
    def clean_markdown(self, text):
        text = re.sub(r"(?si)[\s\.]*thought.*?End of thought", "", text)
        text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL)
        text = text.replace("**", "")
        text = text.replace("### ", "").replace("## ", "")
        if text.startswith("> "): text = text[2:]
        # 去掉 AI 用反引号包裹 MEME 标签的情况: `<MEME:xxx>` → <MEME:xxx>
        text = re.sub(r"`(<MEME:.*?>)`", r"\1", text)
        text = re.sub(r"`(MEME_TAG:\s*\S+)`", r"\1", text)
        return text.strip()

    def smart_split(self, chain):
        segs = []; buf = []
        def flush(): 
            if buf: segs.append(buf[:]); buf.clear()
        
        for c in chain:
            if isinstance(c, Image): flush(); segs.append([c]); continue
            if isinstance(c, Plain):
                txt = c.text; idx = 0; chunk = ""; stack = []
                while idx < len(txt):
                    char = txt[idx]
                    if char in self.pair_map: stack.append(char)
                    elif stack and char == self.pair_map[stack[-1]]: stack.pop()
                    
                    is_split_char = char in self.split_chars
                    force_split = False
                    
                    if (not stack and is_split_char) or force_split:
                        chunk += char
                        if is_split_char:
                            while idx + 1 < len(txt) and txt[idx+1] in self.split_chars: 
                                idx += 1; chunk += txt[idx]
                        if chunk.strip(): buf.append(Plain(chunk))
                        flush(); chunk = ""
                    else: chunk += char
                    idx += 1
                if chunk: buf.append(Plain(chunk))
        flush(); return segs
    
    # ... 下面是 Config/Data/Server 部分 ...

    def load_config(self):
        default = {
            "web_port": 5000, "debounce_time": 3.0,
            "auto_save_cooldown": 60, "ab_context_rounds": 50,
            "proactive_interval": 0,
            "quiet_start": 23, "quiet_end": 7,
            "delay_base": 0.5, "delay_factor": 0.1,
            "web_token": "admin123", # 确保有默认token
            "ai_prompt": 
            """你是一个专业的表情包筛选员，正在帮我扩充图库。
            用户发送图片时的配文是：“{context_text}”。(请结合该配文理解，但如果配文在玩梗，请以图片视觉事实为准)
            
            【核心原则：严禁幻觉与乱联想】
            1. 视觉识别必须精准：实事求是，禁止幻觉和过度联想二次元内容！
            2. 黑名单（遇到以下内容直接回复 NO）：
            - 严禁 米哈游/原神/崩坏等 miHoYo 相关内容。
            - 严禁 辱女、性别歧视、黄色暴力或让人不适的烂梗。
            - 普通的系统截图、无关的风景照、纯文字聊天记录。
            
            【判断逻辑】
            - 只有当图片是有趣的、可爱的、或具有情绪表达价值的表情包（如 Chiikawa、线条小狗、Kpop爱豆表情、猫猫狗狗、经典Meme、梗图）时，才保存。
            
            【输出格式】
            如果不保存，仅回复：NO
            如果保存，请严格按以下格式回复（若认不出请直接用一句话描述，省略名称）：
            YES
            <准确的名称>:一句简短自然的各种场景使用说明""",
            "smtp_host": "", "smtp_user": "", "smtp_pass": "", "email_to": "" # 默认设置为空字符串
        }
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    content = json.load(f)
                    default.update(content)
            except: pass
        return default

    def check_config_reload(self):
        if os.path.exists(self.config_file):
            try:
                mtime = os.path.getmtime(self.config_file)
                if mtime > self.config_mtime:
                    print(f"🔄 [Meme] 配置文件热重载", flush=True)
                    self.local_config = self.load_config()
                    self.config_mtime = mtime   # ← 加这行
            except: pass
            
    async def check_and_summarize(self):
        """v24: 纯粹的总结逻辑 (只生成 Fragment，不重复提取 Sticky)"""
        ab_rounds = int(self.local_config.get("ab_context_rounds", 50))
        if ab_rounds <= 20:
            threshold = ab_rounds
            summary_words = 150
        elif ab_rounds <= 50:
            threshold = int(ab_rounds * 0.8)
            summary_words = 300
        else:
            threshold = 50
            summary_words = 400
        if len(self.chat_history_buffer) < threshold or self.is_summarizing: 
            return
        
        self.is_summarizing = True 
        try:
            print(f"🧠 [Meme] 正在消化记忆 (积压: {len(self.chat_history_buffer)} 条)...", flush=True)
            now_str = self.get_full_time_str()
            # 从 buffer 提取实际消息日期范围
            first_msg = self.chat_history_buffer[0] if self.chat_history_buffer else ""
            date_match = re.search(r'\d{4}-\d{2}-\d{2}', first_msg)
            msg_date = date_match.group() if date_match else now_str.split(' ')[0]
            history_text = "\n".join(self.chat_history_buffer)
            
            provider = self.context.get_using_provider()
            if not provider: return
            
            prompt = f"""当前时间：{now_str}
                这是一段过去的对话记录。请将其总结为简练的"长期记忆"。
                【重要规则】如果对话跨越多天，必须按日期分段总结，格式如：
                [2025-12-29] 发生了xxx
                [2025-12-30] 发生了xxx
                如果同一天则只写一个日期。
                重点记录：用户的喜好、重要事件、双方约定。
                忽略：无意义寒暄、重复表情包指令。
                字数限制：{summary_words}字以内。请确保包含适量的细节关键词以便日后检索。

                对话内容：
                {history_text}"""
            
            resp = await provider.text_chat(prompt, session_id=None)
            summary = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
            # 简单清洗一下
            summary = self.clean_markdown(summary)
            
            if summary:
                # 按行分段存储，每段独立一条 fragment，方便精准检索
                lines = summary.split("\n")
                for line in lines:
                    line = line.strip()
                    if not line or len(line) < 5:
                        continue
                    # 如果这行没有日期前缀，加上日期
                    if not re.match(r"\[?\d{4}", line):
                        line = f"[{msg_date}] {line}"
                    await self.save_message_to_db(line, 'fragment')
                
                # 清空 buffer
                self.chat_history_buffer = [] 
                self.save_buffer_to_disk()
                print(f"✨ [Meme] 总结完成，已存入片段库。", flush=True)

        except Exception as e:
            print(f"❌ [Meme] 总结失败: {e}", flush=True)
        finally:
            self.is_summarizing = False
            
    async def ai_evaluate_image(self, img_url, context_text=""):
        """v24 Ultimate: 并发锁 + 上下文注入 + 数据库锁 + 详细报错"""
        # 1. API 通行证：串行执行防 429
        async with self.api_semaphore:
            try:
                # 下载图片
                img_data = None
                async with aiohttp.ClientSession() as s:
                    async with s.get(img_url, timeout=15) as r:
                        if r.status == 200: 
                            img_data = await r.read()
                        else:
                            print(f"⚠️ [自动进货] 图片下载失败 (HTTP {r.status}): {img_url}")
                            return
                if not img_data: return
                
                # 2. 计算指纹并去重
                current_hash = await self._calc_hash_async(img_data)
                
                conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
                c = conn.cursor()
                c.execute("SELECT filename FROM memes WHERE feature_hash = ? AND feature_hash != ''", (current_hash,))
                exists = c.fetchone()
                conn.close()
                
                if exists:
                    print(f"♻️ [自动进货] 指纹匹配，跳过重复图")
                    return

                # 3. 准备 AI 鉴图
                provider = self.context.get_using_provider()
                if not provider:
                    print("❌ [自动进货] 错误: 无法获取 AI Provider")
                    return
                
                raw_prompt = self.local_config.get("ai_prompt", "")
                # 注入用户发图时的配文上下文
                prompt = raw_prompt.replace("{context_text}", context_text) if "{context_text}" in raw_prompt else raw_prompt
                
                resp = await provider.text_chat(prompt, session_id=None, image_urls=[img_url])
                content = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
                
                # 4. 处理 AI 回复
                if "YES" in content:
                    match = re.search(r"<?(?P<tag>[^>\n:：]+)>?[:：]\s*(?P<desc>.*)", content)
                    if match:
                        full_tag = f"{match.group('tag').strip()}: {match.group('desc').strip()}"
                        print(f"🖤 [自动进货] 识图成功: {full_tag}")
                        
                        comp, ext = await self._compress_image(img_data)
                        fn = f"{int(time.time())}_{random.randint(100,999)}{ext}"
                        with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp)
                        
                        # 5. 写入数据库 (加异步锁)
                        async with self.db_lock:
                            try:
                                conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
                                c = conn.cursor()
                                c.execute("INSERT INTO memes (filename, tags, source, feature_hash, created_at) VALUES (?, ?, 'auto', ?, ?)", 
                                          (fn, full_tag, current_hash, time.time()))
                                conn.commit()
                                conn.close()
                            except sqlite3.Error as db_err:
                                print(f"❌ [自动进货] 数据库写入失败: {db_err}")
                    else:
                        print(f"⚠️ [自动进货] AI 同意存图但格式解析失败: {content[:50]}...")
                elif "NO" in content:
                    # AI 判定不存，这是正常逻辑，不用打印错误
                    pass
                else:
                    print(f"⚠️ [自动进货] AI 回复内容未包含 YES/NO: {content[:50]}...")

            except asyncio.TimeoutError:
                print(f"❌ [自动进货] 请求超时 (网络问题)")
            except Exception as e:
                # 打印所有未预见的异常
                print(f"❌ [自动进货] 识图任务执行出错: {e}")



    async def _lonely_watcher(self):
        while self.running: 
            await asyncio.sleep(60)
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            hour = datetime.datetime.now().hour
            if hour == 5 and today != self.last_email_date:
                if self.local_config.get("smtp_host"):
                    try:
                        await self.send_backup_email()
                        self.last_email_date = today
                        print("📧 [Meme] 每日备份邮件已发送", flush=True)
                    except: pass
 
            self.check_config_reload()
            
            interval = self.local_config.get("proactive_interval", 0)
            if interval <= 0: continue
            
            q_start = self.local_config.get("quiet_start", -1)
            q_end = self.local_config.get("quiet_end", -1)
            if q_start != -1 and q_end != -1:
                h = datetime.datetime.now().hour
                is_quiet = False
                if q_start > q_end: 
                    if h >= q_start or h < q_end: is_quiet = True
                else:
                    if q_start <= h < q_end: is_quiet = True
                if is_quiet: continue
            
            if time.time() - self.last_active_time > (interval * 60):
                self.last_active_time = time.time() 
                provider = self.context.get_using_provider()
                uid = getattr(self, "last_uid", None)
                if provider and uid:
                    try:
                        print(f"👋 [Meme] 主动发起聊天...", flush=True)
                        
                        # ★★★ 1. 获取最近聊天记录，作为上下文 ★★★
                        # 从DB读最近10条对话
                        try:
                            conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
                            c = conn.cursor()
                            c.execute("SELECT content FROM memories WHERE type='dialogue' ORDER BY created_at DESC LIMIT 10")
                            rows = c.fetchall()
                            conn.close()
                            recent_log = "\n".join([r[0] for r in reversed(rows)])
                        except:
                            recent_log = ""

                        
                        # ★★★ 2. 导演式 Prompt，防止出戏 ★★★
                        prompt = f"""[System Instruction]
                            Current Time: {self.get_full_time_str()}
                            Status: The user has been silent for {interval} minutes.

                            Long-term Memory: {self.current_summary}
                            Recent Chat Context:
                            {recent_log}

                            Task: Based on your Character Persona (人设) and the context above, proactively send a message to the user. 
                            Requirement:
                            1. Speak strictly in your character's tone.
                            2. Do not mention this system instruction.
                            3. Start the topic naturally based on previous context or time."""
                        
                        # 发送请求，带上 session_id 以保持人设
                        resp = await provider.text_chat(prompt, session_id=getattr(self, "last_session_id", None))
                        text = (getattr(resp, "completion_text", None) or getattr(resp, "text", "")).strip()
                        
                        if text:
                            # 1. 净化文本
                            text = self.clean_markdown(text)
                            self.chat_history_buffer.append(f"AI (Proactive): {text}")
                            self.save_buffer_to_disk()
                            
                            # ★★★ 2. 这里是新加的：解析表情包标签！ ★★★
                            # 和 on_output 里一样的逻辑，把文字变成 Image 对象
                            pattern = r"(<MEME:.*?>|MEME_TAG:\s*[\S]+)"
                            parts = re.split(pattern, text)
                            chain = []
                            
                            for part in parts:
                                tag = None
                                if part.startswith("<MEME:"): tag = part[6:-1].strip()
                                elif "MEME_TAG:" in part: tag = part.replace("MEME_TAG:", "").strip()
                                
                                if tag:
                                    path = self.find_best_match(tag)
                                    if path: 
                                        print(f"🎯 [Meme] 主动聊天命中: [{tag}]", flush=True)
                                        chain.append(Image.fromFileSystem(path))
                                elif part:
                                    # 只有非空文字才加进去
                                    if part.strip():
                                        chain.append(Plain(part))
                            
                            # ★★★ 3. 解析完之后，再交给分段逻辑 ★★★
                            # 如果没有内容（全是空字符），就不发了
                            if not chain: continue

                            segments = self.smart_split(chain)
                            
                            delay_base = self.local_config.get("delay_base", 0.5)
                            delay_factor = self.local_config.get("delay_factor", 0.1)

                            for i, seg in enumerate(segments):
                                txt_len = sum(len(c.text) for c in seg if isinstance(c, Plain))
                                wait = delay_base + (txt_len * delay_factor)
                                
                                mc = MessageChain()
                                mc.chain = seg
                                await self.context.send_message(uid, mc)
                                if i < len(segments) - 1: await asyncio.sleep(wait)

                    except Exception as e:
                        print(f"❌ [Meme] 主动聊天出错: {e}", flush=True)

    async def _init_image_hashes(self):
        if not os.path.exists(self.img_dir): return
        count = 0
        for f in os.listdir(self.img_dir):
            if not f.lower().endswith(('.jpg', '.png', '.jpeg', '.gif', '.webp')): continue
            if f in self.data and 'hash' in self.data[f] and self.data[f]['hash']:
                self.img_hashes[f] = self.data[f]['hash']
                continue
            try:
                path = os.path.join(self.img_dir, f)
                with open(path, "rb") as fl: content = fl.read()
                h = await self._calc_hash_async(content)
                if h: 
                    self.img_hashes[f] = h
                    if f not in self.data: self.data[f] = {"tags": "未分类", "source": "unknown"}
                    self.data[f]['hash'] = h
                    count += 1
            except: pass
        self.save_data()
        print(f"✅ [Meme] 指纹库加载完毕，有效图片: {len(self.img_hashes)}", flush=True)

    async def _calc_hash_async(self, image_data):
        def _sync():
            try:
                img = PILImage.open(io.BytesIO(image_data))
                if getattr(img, 'is_animated', False): img.seek(0)
                img = img.resize((9, 8), PILImage.Resampling.LANCZOS).convert('L')
                pixels = list(img.getdata())
                val = sum(2**i for i, v in enumerate([pixels[row*9+col] > pixels[row*9+col+1] for row in range(8) for col in range(8)]) if v)
                return hex(val)[2:]
            except: return None
        return await asyncio.get_running_loop().run_in_executor(self.executor, _sync)

    async def _compress_image(self, image_data: bytes):
        def _sync():
            try:
                img = PILImage.open(io.BytesIO(image_data))
                if getattr(img, 'is_animated', False): return image_data, ".gif"
                max_w = 400
                if img.width > max_w:
                    ratio = max_w / img.width
                    img = img.resize((max_w, int(img.height * ratio)), PILImage.Resampling.LANCZOS)
                buf = io.BytesIO()
                if img.mode != "RGB": img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=75)
                return buf.getvalue(), ".jpg"
            except: return image_data, ".jpg"
        return await asyncio.get_running_loop().run_in_executor(self.executor, _sync)

    def _get_all_img_urls(self, e):
        urls = []
        if not e.message_obj or not e.message_obj.message: return urls
        for c in e.message_obj.message:
            if isinstance(c, Image): urls.append(c.url)
        return urls

    def find_best_match(self, query):
        """从 SQLite 查找最佳匹配的表情包文件路径"""
        # 1. 尝试直接查库
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        c.execute("SELECT filename FROM memes WHERE tags LIKE ? LIMIT 1", (f"%{query}%",))
        row = c.fetchone()
        conn.close()
    
        if row:
            print(f"🔍 [Meme Match] DB精确命中: query='{query}' → {row[0]}", flush=True)
            return os.path.join(self.img_dir, row[0])
        
        # 2. 如果库里没查到，再遍历 self.data
        threshold = float(self.local_config.get("meme_match_threshold", 0.3))  # ← 改动A：读配置
        best, score = None, 0
        for f, i in self.data.items():
            t = i.get("tags", "")
            if query in t:
                print(f"🔍 [Meme Match] data精确命中: query='{query}' → {f}", flush=True)
                return os.path.join(self.img_dir, f)
            name = t.split(":")[0] if ":" in t else t          # ← 改动B：拆出名字
            desc = t.split(":")[-1] if ":" in t else ""        # ← 拆出描述
            s = max(                                            # ← 改动C：两个都比
                difflib.SequenceMatcher(None, query, name).ratio(),
                difflib.SequenceMatcher(None, query, desc).ratio()
            )
            if s > score: score = s; best = f
        
        print(f"🔍 [Meme Match] 模糊匹配: query='{query}', 最佳={best}, 分数={score:.2f}, 阈值={threshold}", flush=True)
        if score >= threshold: return os.path.join(self.img_dir, best)  # ← 0.4 改成 threshold
        return None

    
    def save_config(self): 
        try: json.dump(self.local_config, open(self.config_file,"w"), indent=2)
        except: pass
    def load_data(self): return json.load(open(self.data_file)) if os.path.exists(self.data_file) else {}
    def save_data(self): json.dump(self.data, open(self.data_file,"w"), ensure_ascii=False)
    def load_buffer_from_disk(self):
        try: return json.load(open(self.buffer_file, "r"))
        except: return []
    def save_buffer_to_disk(self):
        try: json.dump(self.chat_history_buffer, open(self.buffer_file, "w"), ensure_ascii=False)
        except: pass
    def load_memory(self):
        try: return open(self.memory_file, "r", encoding="utf-8").read()
        except: return ""
    def read_file(self, n): 
        try: return open(os.path.join(self.base_dir, n), "r", encoding="utf-8").read()
        except: return ""
    def check_auth(self, r): return r.query.get("token") == self.local_config.get("web_token")

    def get_full_time_str(self):
        now = datetime.datetime.now()
        time_str = now.strftime('%Y-%m-%d %H:%M')
        if HAS_LUNAR:
            try:
                lunar = Solar.fromYmdHms(now.year, now.month, now.day, now.hour, now.minute, now.second).getLunar()
                time_str += f" (农历{lunar.getMonthInChinese()}月{lunar.getDayInChinese()})"
            except: pass
        return time_str

    async def start_web_server(self):
        app = web.Application()
        app._client_max_size = 100 * 1024 * 1024 
        app.router.add_get("/", self.h_idx)
        app.router.add_post("/upload", self.h_up)
        app.router.add_post("/batch_delete", self.h_del)
        app.router.add_post("/update_tag", self.h_tag)
        app.router.add_get("/get_config", self.h_gcf)
        app.router.add_post("/update_config", self.h_ucf)
        app.router.add_get("/backup", self.h_backup)
        app.router.add_post("/restore", self.h_restore)
        app.router.add_post("/slim_images", self.h_slim)
        app.router.add_post("/test_email", self.h_test_email)
        app.router.add_post("/import_legacy", self.h_import_legacy) # <--- 新加的
        app.router.add_get("/get_stickies", self.h_get_stickies) # <--- 新加
        app.router.add_post("/update_sticky", self.h_update_sticky) # <--- 新加
        app.router.add_static("/images/", path=self.img_dir)
        app.router.add_get("/meme_count", self.h_meme_count)
        # === DB Management API ===
        app.router.add_get("/api/db/list", self.h_db_list)
        app.router.add_post("/api/db/delete", self.h_db_delete)
        app.router.add_post("/api/db/edit", self.h_db_edit)
        runner = web.AppRunner(app)
        await runner.setup()
        port = self.local_config.get("web_port", 5000)
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"🌐 [Meme] WebUI 管理后台已启动: http://localhost:{port}", flush=True)


    async def h_up(self, r):
        """上传接口：直接写入 SQLite"""
        if not self.check_auth(r): return web.Response(status=403)
        rd = await r.multipart(); tag = "未分类"
        
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        
        while True:
            p = await rd.next()
            if not p: break
            if p.name == "tags": tag = await p.text()
            elif p.name == "file":
                raw = await p.read()
                # 1. 压缩保存
                comp, ext = await self._compress_image(raw)
                fn = f"{int(time.time()*1000)}_{random.randint(100,999)}{ext}"
                with open(os.path.join(self.img_dir, fn), "wb") as f: f.write(comp)
                
                # 2. 计算 Hash
                h = await self._calc_hash_async(comp) 
                
                # 3. 写入数据库
                try:
                    c.execute("INSERT INTO memes (filename, tags, source, feature_hash, created_at) VALUES (?, ?, 'manual', ?, ?)",
                              (fn, tag, h, time.time()))
                except sqlite3.IntegrityError: pass # 忽略重复文件名
        
        conn.commit()
        conn.close()
        return web.Response(text="ok")

    async def h_del(self, r):
        """删除接口：同步删除文件和数据库记录"""
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        filenames = data.get("filenames", [])
        
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        
        for f in filenames:
            # 删文件
            try: os.remove(os.path.join(self.img_dir, f))
            except: pass
            # 删库
            c.execute("DELETE FROM memes WHERE filename=?", (f,))
            
        conn.commit()
        conn.close()
        return web.Response(text="ok")

    async def h_tag(self, r):
        """修改标签接口"""
        if not self.check_auth(r): return web.Response(status=403)
        d = await r.json()
        
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        c.execute("UPDATE memes SET tags=? WHERE filename=?", (d['tags'], d['filename']))
        conn.commit()
        conn.close()
        
        return web.Response(text="ok")

    async def h_idx(self, r):
        """首页：从数据库读取列表，而不是 self.data"""
        if not self.check_auth(r): return web.Response(status=403, text="Need ?token=xxx")
        
        # 从数据库捞所有图
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT filename, tags, source FROM memes ORDER BY created_at DESC")
        rows = c.fetchall()
        conn.close()
        
        # 转成 dict 格式喂给前端 (兼容旧 html 结构)
        # 结构: {"filename": {"tags": "xxx", "source": "manual"}, ...}
        data_for_web = {row['filename']: {"tags": row['tags'], "source": row['source']} for row in rows}
        
        token = self.local_config["web_token"]
        html = self.read_file("index.html").replace("{{ MEME_DATA }}", json.dumps(data_for_web)).replace("admin123", token)
        return web.Response(text=html, content_type="text/html")
    async def h_gcf(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        return web.json_response(self.local_config)

    async def h_ucf(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        try:
            new_conf = await r.json()
            for k, v in new_conf.items():
                if k in ['web_token', 'ai_prompt', 'smtp_host', 'smtp_user', 'smtp_pass', 'email_to']:
                    # 关键修复：如果 v 是 None 或者 字符串 "None"，就存为空字符串
                    val = str(v) if v is not None else ""
                    if val.lower() == "none": val = ""
                    self.local_config[k] = val
                else:
                    try:
                        if v is not None and str(v).strip() != "" and str(v).lower() != "none":
                            self.local_config[k] = float(v)
                    except: pass
            self.save_config()
            return web.Response(text="ok")
        except Exception as e:
            return web.Response(status=500, text=str(e))

    async def h_backup(self,r):
        if not self.check_auth(r): return web.Response(status=403)
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
            for root,_,files in os.walk(self.img_dir): 
                for f in files: z.write(os.path.join(root,f),f"images/{f}")
            if os.path.exists(self.data_file): z.write(self.data_file,"memes.json")
            if os.path.exists(self.config_file): z.write(self.config_file,"config.json")
            if os.path.exists(self.buffer_file): z.write(self.buffer_file, "buffer.json")
            db_p = os.path.join(self.base_dir, "meme_core.db")
            if os.path.exists(db_p): z.write(db_p, "meme_core.db")
        b.seek(0)
        return web.Response(body=b, headers={'Content-Disposition':'attachment; filename="meme_backup.zip"'})

    async def h_restore(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        try:
            reader = await r.multipart()
            field = await reader.next()
            file_data = await field.read()
            
            response = web.StreamResponse()
            response.content_type = 'text/plain'
            await response.prepare(r)

            def unzip_action():
                with zipfile.ZipFile(io.BytesIO(file_data), 'r') as z: 
                    z.extractall(self.base_dir)
            
            loop = asyncio.get_running_loop()
            task = loop.create_task(loop.run_in_executor(self.executor, unzip_action))
            
            # 发送心跳包保持连接，防止 Cloudflare 等反代的 524 超时
            while not task.done():
                try:
                    await response.write(b" ")
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            
            await task
            
            # 重新加载数据
            self.data = self.load_data()
            self.local_config = self.load_config()
            self.chat_history_buffer = self.load_buffer_from_disk()
            self.config_mtime = os.path.getmtime(self.config_file) if os.path.exists(self.config_file) else 0
            self.round_count = 0
            self.init_db()
            
            await response.write(b"ok")
            await response.write_eof()
            return response
        except Exception as e:
            return web.Response(status=500, text=str(e))

    async def h_import_legacy(self, r):
        """WebUI 接口：接收旧数据文件并导入"""
        if not self.check_auth(r): return web.Response(status=403, text="Forbidden")
        try:
            reader = await r.multipart()
            memes_data, memory_text, buffer_data = None, "", []

            while True:
                field = await reader.next()
                if not field: break
                
                # 读取并解码
                content = await field.read()
                if not content: continue
                
                if field.name == 'memes_json':
                    try: memes_data = json.loads(content.decode('utf-8'))
                    except: pass
                elif field.name == 'memory_txt':
                    memory_text = content.decode('utf-8')
                elif field.name == 'buffer_json':
                    try: buffer_data = json.loads(content.decode('utf-8'))
                    except: pass

            success, msg = self.merge_legacy_data(memes_data, memory_text, buffer_data)
            return web.Response(text=msg if success else "Error: " + msg)
            
        except Exception as e:
            return web.Response(status=500, text=f"Server Error: {str(e)}")

    async def h_get_stickies(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        conn.row_factory = sqlite3.Row # 让我们能用字段名取数据
        c = conn.cursor()
        # 只取 sticky 类型的记忆，按时间倒序
        c.execute("SELECT id, content, created_at FROM memories WHERE type='sticky' ORDER BY created_at DESC")
        rows = [dict(ix) for ix in c.fetchall()]
        conn.close()
        return web.json_response(rows)

    async def h_update_sticky(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        action = data.get('action') # add / delete / edit
        
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        
        try:
            if action == 'add':
                content = data.get('content', '').strip()
                if content:
                    # 插入一条 sticky 记忆，权重设为 10 (最高)
                    c.execute("INSERT INTO memories (content, type, importance, created_at) VALUES (?, 'sticky', 10, ?)", 
                             (content, time.time()))
            
            elif action == 'delete':
                mid = data.get('id')
                if mid:
                    c.execute("DELETE FROM memories WHERE id=? AND type='sticky'", (mid,))
            
            elif action == 'edit':
                mid = data.get('id')
                content = data.get('content', '').strip()
                if mid and content:
                    c.execute("UPDATE memories SET content=? WHERE id=? AND type='sticky'", (content, mid))
            
            conn.commit()
            # 用同一个连接写 config，避免新连接锁冲突
            c.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('round_count', '0')")
            c.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('sticky_updated', '1')")
            conn.commit()
            print(f"🔍 [Sticky] WebUI更新，已设置 sticky_updated=1, round_count=0", flush=True)
            return web.Response(text="ok")
        except Exception as e:
            return web.Response(status=500, text=str(e))
        finally:
            conn.close()
            
    async def h_slim(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        loop = asyncio.get_running_loop()
        count = 0
        for f in os.listdir(self.img_dir):
            try:
                p = os.path.join(self.img_dir, f)
                with open(p, 'rb') as fl: raw = fl.read()
                nd, _ = await self._compress_image(raw)
                if len(nd) < len(raw):
                    with open(p, 'wb') as fl: fl.write(nd)
                    count += 1
            except: pass
        return web.Response(text=f"优化了 {count} 张")
        
    async def h_test_email(self, r):
        if not self.check_auth(r): return web.Response(status=403)
        res = await self.send_backup_email()
        return web.Response(text=res)

    async def h_meme_count(self, r):
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM memes")
        count = c.fetchone()[0]
        conn.close()
        return web.json_response({"count": count})

    # === DB Management Endpoints ===
    async def h_db_list(self, r):
        """列出 memories 表记录 (分页 + 类型筛选 + 搜索)"""
        if not self.check_auth(r): return web.Response(status=403)
        mem_type = r.query.get("type", "dialogue")  # dialogue/fragment/sticky
        page = int(r.query.get("page", 1))
        search = r.query.get("search", "").strip()
        page_size = 30
        offset = (page - 1) * page_size
        
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        if search:
            c.execute("SELECT COUNT(*) FROM memories WHERE type=? AND content LIKE ?", (mem_type, f"%{search}%"))
        else:
            c.execute("SELECT COUNT(*) FROM memories WHERE type=?", (mem_type,))
        total = c.fetchone()[0]
        
        if search:
            c.execute("SELECT id, content, keywords, created_at FROM memories WHERE type=? AND content LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                     (mem_type, f"%{search}%", page_size, offset))
        else:
            c.execute("SELECT id, content, keywords, created_at FROM memories WHERE type=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                     (mem_type, page_size, offset))
        rows = [{"id": r["id"], "content": r["content"], "keywords": r["keywords"] or "", 
                 "created_at": r["created_at"]} for r in c.fetchall()]
        conn.close()
        
        return web.json_response({"total": total, "page": page, "page_size": page_size, "rows": rows})

    async def h_db_delete(self, r):
        """删除指定 memories 记录"""
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        ids = data.get("ids", [])
        if not ids: return web.json_response({"ok": False, "msg": "no ids"})
        
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        placeholders = ",".join(["?"] * len(ids))
        c.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        conn.commit()
        deleted = c.rowcount
        conn.close()
        return web.json_response({"ok": True, "deleted": deleted})

    async def h_db_edit(self, r):
        """编辑指定 memories 记录的 content"""
        if not self.check_auth(r): return web.Response(status=403)
        data = await r.json()
        mid = data.get("id")
        content = data.get("content", "").strip()
        if not mid or not content: return web.json_response({"ok": False, "msg": "missing id or content"})
        
        # 重新提取关键词
        kw = " ".join(jieba.analyse.extract_tags(content, topK=8))
        
        conn = sqlite3.connect(os.path.join(self.base_dir, "meme_core.db"))
        c = conn.cursor()
        c.execute("UPDATE memories SET content=?, keywords=? WHERE id=?", (content, kw, mid))
        conn.commit()
        updated = c.rowcount
        conn.close()
        return web.json_response({"ok": True, "updated": updated})

    async def send_backup_email(self):
        conf = self.local_config
        host = conf.get("smtp_host")
        user = conf.get("smtp_user")
        pw = conf.get("smtp_pass")
        to_email = conf.get("email_to")
        
        if not all([host, user, pw, to_email]): return "配置不全：请检查SMTP主机、账号、授权码和收件人"

        try:
            # 1. 计算大小
            img_size = 0
            for root, _, files in os.walk(self.img_dir):
                img_size += sum(os.path.getsize(os.path.join(root, name)) for name in files)
            
            include_images = img_size < (20 * 1024 * 1024)
            msg_body = "MemeMaster 自动备份。\n"
            
            # 2. 制作 ZIP
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as z:
                db_path = os.path.join(self.base_dir, "meme_core.db")
                if os.path.exists(db_path): z.write(db_path, "meme_core.db")
                if os.path.exists(self.config_file): z.write(self.config_file, "config.json")
                
                if include_images:
                    for root, _, files in os.walk(self.img_dir):
                        for f in files: z.write(os.path.join(root, f), f"images/{f}")
                    msg_body += "✅ 包含完整图片库。"
                else:
                    z.writestr("README.txt", "图片库过大，未包含。")
                    msg_body += f"⚠️ 图片库过大 ({img_size/1024/1024:.1f}MB)，仅备份了数据库和配置。"
            
            zip_buffer.seek(0)

            # 3. 组装邮件 (这里就是之前缺少的步骤)
            msg = MIMEMultipart()
            msg['Subject'] = f"MemeMaster 备份 - {datetime.datetime.now().strftime('%Y-%m-%d')}"
            msg['From'] = user
            msg['To'] = to_email
            
            # 添加正文
            msg.attach(MIMEText(msg_body, 'plain', 'utf-8'))
            
            # 添加附件
            part = MIMEBase('application', "octet-stream")
            part.set_payload(zip_buffer.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment; filename="meme_backup.zip"')
            msg.attach(part)

            # 4. 发送
            def _send():
                with smtplib.SMTP_SSL(host, 465) as server:
                    server.login(user, pw)
                    server.send_message(msg)
            
            await asyncio.get_running_loop().run_in_executor(self.executor, _send)
            return "✅ 备份邮件已发送，请查收"

        except Exception as e:
            return f"❌ 发送失败: {str(e)}"
