import re
import quart
from typing import Dict

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.platform import MessageType, AstrBotMessage

try:
    from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_server import QQOfficialWebhook
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import QQOfficialPlatformAdapter
    CAN_PATCH = True
except ImportError:
    logger.error("NicknameCapturer: 无法导入核心模块，猴子补丁无法生效。请检查 AstrBot 版本。")
    CAN_PATCH = False

@register(
    "Nickname Capturer",
    "Magstic, Cascade",
    "在 QQ 官方机器人平台从用户回复中捕获其真实昵称，并修正事件对象。",
    "3.0.0-final",
)
class NicknameCapturer(Star):
    _original_qq_webhook_init = None
    _original_parser = None
    # 并发安全的数据中转站
    _temp_raw_data_map: Dict[str, dict] = {}

    def __init__(self, context: Context):
        super().__init__(context)
        self.nickname_cache: Dict[str, str] = {}
        self.initialize_patch()
        logger.info("Nickname Capturer 插件已加载")

    def initialize_patch(self):
        """初始化并应用基于 __init__ 和 ID 映射的、并发安全的终极猴子补丁"""
        if not CAN_PATCH:
            return

        logger.info("NicknameCapturer: 正在应用并发安全的终极猴子补丁...")

        NicknameCapturer._original_qq_webhook_init = QQOfficialWebhook.__init__
        NicknameCapturer._original_parser = QQOfficialPlatformAdapter._parse_from_qqofficial

        def _patched_init(qq_webhook_instance, *args, **kwargs):
            NicknameCapturer._original_qq_webhook_init(qq_webhook_instance, *args, **kwargs)
            logger.info("NicknameCapturer (patch): 已捕获 QQOfficialWebhook 实例并完成原始初始化。")

            async def new_callback():
                try:
                    msg: dict = await quart.request.json
                    raw_d = msg.get('d', {})
                    message_id = raw_d.get('id')

                    if message_id:
                        logger.debug(f"NicknameCapturer (patch): 正在为消息 {message_id} 暂存原始数据")
                        NicknameCapturer._temp_raw_data_map[message_id] = raw_d

                    opcode = msg.get("op")
                    if opcode == 13:  # Validation event
                        signed = await qq_webhook_instance.webhook_validation(raw_d)
                        return signed

                    event_type = msg.get("t")
                    if event_type and opcode == 0:  # Dispatch event
                        try:
                            func = qq_webhook_instance._connection.parser[event_type.lower()]
                            func(msg)
                        except KeyError:
                            logger.error(f"NicknameCapturer (patch): _parser unknown event {event_type.lower()}.")
                    
                    return quart.Response(status=204)
                except Exception as e:
                    logger.error(f"NicknameCapturer (new_callback): 补丁回调中发生严重错误: {e}", exc_info=True)
                    return quart.Response(response="Internal Server Error", status=500)

            qq_webhook_instance.server.view_functions['callback'] = new_callback
            logger.info("NicknameCapturer (patch): 已成功将 Quart 路由指向新的、并发安全的回调函数。")

        try:
            QQOfficialWebhook.__init__ = _patched_init
            QQOfficialPlatformAdapter._parse_from_qqofficial = NicknameCapturer._patched_parser
            logger.info("NicknameCapturer: 基于 __init__ 和 ID 映射的终极猴子补丁应用成功。")
        except Exception as e:
            logger.error(f"NicknameCapturer: 应用猴子补丁时发生错误: {e}", exc_info=True)

    @classmethod
    async def terminate(cls):
        """插件停用/重载时，恢复所有猴子补丁，遵循 AstrBot 开发规范。"""
        if not CAN_PATCH:
            return
        logger.info("NicknameCapturer: 正在恢复所有猴子补丁...")
        try:
            # 恢复的是类级别的补丁，所以使用类属性
            if hasattr(cls, '_original_qq_webhook_init') and cls._original_qq_webhook_init:
                QQOfficialWebhook.__init__ = cls._original_qq_webhook_init
                cls._original_qq_webhook_init = None
            if hasattr(cls, '_original_parser') and cls._original_parser:
                QQOfficialPlatformAdapter._parse_from_qqofficial = cls._original_parser
                cls._original_parser = None
            
            # 清理类级别的缓存
            if hasattr(cls, '_temp_raw_data_map'):
                cls._temp_raw_data_map.clear()
                
            logger.info("NicknameCapturer: 所有猴子补丁已恢复，临时数据已清空。")
        except Exception as e:
            logger.error(f"NicknameCapturer: 恢复猴子补丁时发生错误: {e}", exc_info=True)

    @staticmethod
    def _patched_parser(message, message_type) -> AstrBotMessage:
        msg_obj = NicknameCapturer._original_parser(message, message_type)
        message_id = getattr(message, 'id', None)
        
        if message_id and message_id in NicknameCapturer._temp_raw_data_map:
            logger.info(f"NicknameCapturer (patch): 检测到消息 {message_id} 的暂存数据，正在注入...")
            raw_data_d = NicknameCapturer._temp_raw_data_map.pop(message_id)
            setattr(msg_obj, 'raw_qq_webhook_d', raw_data_d)
        return msg_obj

    @filter.regex(r".*", priority=-10)
    async def capture_and_patch_nickname(self, event: AstrMessageEvent):
        logger.info("--- NicknameCapturer: 开始处理事件 ---")
        try:
            is_qq_official_group = (
                event.platform_meta.name == "qq_official_webhook"
                and event.message_obj.type == MessageType.GROUP_MESSAGE
            )
            if not is_qq_official_group:
                return # 静默处理，避免刷屏
            logger.info("-> 步骤 1/8 [通过]: 事件为 QQ 官方 Webhook 群消息。")

            user_id = event.message_obj.sender.user_id
            if not user_id:
                logger.warning("-> 步骤 2/8 [终止]: 无法获取 user_id。")
                return
            logger.info(f"-> 步骤 2/8 [通过]: 获取到 user_id: {user_id}")

            raw_data_d = getattr(event.message_obj, 'raw_qq_webhook_d', None)
            if not raw_data_d:
                logger.info("-> 步骤 3/8 [终止]: 'raw_qq_webhook_d' 属性不存在。可能不是回复消息。")
                return
            logger.info("-> 步骤 3/8 [通过]: 成功获取 'raw_qq_webhook_d'。")

            parallel_message = raw_data_d.get('parallel_message')
            if not parallel_message:
                logger.info("-> 步骤 4/8 [终止]: 'parallel_message' 不在原始数据中。")
                return
            logger.info("-> 步骤 4/8 [通过]: 获取到 'parallel_message'。")

            msg_nodes = parallel_message.get('msg_nodes', [])
            if not msg_nodes:
                logger.warning("-> 步骤 5/8 [终止]: 'msg_nodes' 为空或不存在。")
                return
            logger.info("-> 步骤 5/8 [通过]: 获取到 'msg_nodes'。")

            reply_content = msg_nodes[0].get('content', '')
            logger.info(f"-> 步骤 6/8: 提取到回复内容: '{reply_content}'")

            match = re.search(r"@(\S+)", reply_content)
            if not match:
                logger.info("-> 步骤 7/9 [终止]: 正则表达式未匹配到 '@昵称'。")
                return
            
            captured_nickname = match.group(1).strip()
            logger.info(f"-> 步骤 7/9 [通过]: 正则表达式匹配到昵称: '{captured_nickname}'")

            # 终极修正: 必须修补 sender.nickname 属性，而不是 sender.name
            event.message_obj.sender.nickname = captured_nickname
            logger.info(f"-> 步骤 8/9 [修补成功]: 已将事件中用户 {user_id} 的 sender.nickname 修补为 '{captured_nickname}'")

            # 然后，再独立处理缓存逻辑
            if self.nickname_cache.get(user_id) != captured_nickname:
                self.nickname_cache[user_id] = captured_nickname
                logger.info(f"-> 步骤 9/9 [更新缓存]: 用户 {user_id} 的新昵称 '{captured_nickname}' 已更新至缓存。")
            else:
                logger.info(f"-> 步骤 9/9 [缓存命中]: 用户 {user_id} 的昵称 '{captured_nickname}' 已在缓存中，无需更新。")

        except Exception as e:
            logger.error(f"--- NicknameCapturer: 处理过程中发生意外错误 ---: {e}", exc_info=True)
        finally:
            logger.info("--- NicknameCapturer: 事件处理结束 ---")

    def terminate(self):
        """插件停用时清理资源，恢复猴子补丁"""
        if not CAN_PATCH or NicknameCapturer._original_callback is None:
            return
        
        logger.info("NicknameCapturer: 正在恢复猴子补丁...")
        QQOfficialWebhook.callback = NicknameCapturer._original_callback
        botClient.on_group_at_message_create = NicknameCapturer._original_handler
        
        self.nickname_cache.clear()
        logger.info("Nickname Capturer 插件已停用，昵称缓存已清空，猴子补丁已恢复。")
