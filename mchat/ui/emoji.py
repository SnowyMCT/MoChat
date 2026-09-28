"""表情数据与搜索（纯本地：Unicode 表情字符 + 手写关键词表，不联网）。

为什么用 Unicode 字符而不是图片
--------------------------------
* 零网络：键盘能打出来的字符就能插进消息，`QLabel` 用系统彩色字体
  （Windows 上是 "Segoe UI Emoji"）直接画成彩色，不用打包贴图。
* 消息文本里存的就是普通字符串，拷贝到别处、发到别处都不会坏。

例外是**旗帜**：Windows 的字体不画国旗（只画成字母对），所以旗帜走本地精灵图
（:mod:`mchat.ui.flags`，由 ``tools/build_flags.py`` 生成一次），
字符在这里仍然只是"查槽位用的 key"。

表结构
------
``RAW`` 里每个分类是一段紧凑文本，一行一个表情，行首是字符、后面是关键词
（中英混排，搜索时按子串匹配）。这样加表情就是加一行，不用改代码。

分类顺序与图标参考 Telegram 的表情面板。旗帜（``FLAGS``）是唯一的例外：
Windows 的 Segoe UI Emoji 把国旗渲染成两个字母（"CN"），所以它走图片方案。
"""
from __future__ import annotations

from . import flags as flag_data

RECENT = "recent"
FACES = "faces"
ANIMALS = "animals"
FOOD = "food"
ACTIVITY = "activity"
TRAVEL = "travel"
OBJECTS = "objects"
SYMBOLS = "symbols"
FLAGS = "flags"

# (键, 界面标签, 分类图标)
CATEGORIES: tuple[tuple[str, str, str], ...] = (
    (RECENT, "最近", "🕘"),
    (FACES, "表情", "😀"),
    (ANIMALS, "动物自然", "🐻"),
    (FOOD, "食物饮品", "🍔"),
    (ACTIVITY, "活动运动", "⚽"),
    (TRAVEL, "旅行地点", "🚗"),
    (OBJECTS, "物品", "💡"),
    (SYMBOLS, "符号", "❤️"),
    (FLAGS, "旗帜", "🏁"),
)

RAW: dict[str, str] = {
    FACES: """
😀 grin 笑脸 开心 smile
😃 smile 大笑 笑
😄 happy 开心 笑
😁 beam 咧嘴笑 得意 笑
😆 laugh 大笑 哈哈
😅 sweat_smile 冷汗 尴尬 笑
🤣 rofl 笑死 爆笑 打滚
😂 joy 笑哭 笑 眼泪
🙂 slight_smile 微笑 笑 礼貌
🙃 upside_down 倒脸 微笑
😉 wink 眨眼 抛媚眼 笑
😊 blush 害羞 笑 微笑
😇 innocent 天使 无辜 光环
🥰 love 爱 喜欢 心
😍 heart_eyes 爱慕 喜欢 心动
🤩 star_struck 星星眼 惊艳 崇拜
😘 kiss 亲吻 飞吻 爱
😗 kissing 亲 亲吻
☺️ relaxed 微笑 开心
😋 yum 好吃 馋 美味
😛 tongue 吐舌 调皮
😜 wink_tongue 吐舌 眨眼 顽皮
🤪 zany 搞怪 疯 滑稽
😝 squint_tongue 吐舌 鬼脸
🤑 money_mouth 财迷 钱 有钱
🤗 hug 拥抱 抱抱 温暖
🤭 hand_over_mouth 捂嘴 偷笑 憋笑
🤫 shush 嘘 安静 保密
🤔 thinking 思考 想 考虑 疑问
🤐 zipper 闭嘴 保密 不说
🤨 raised_eyebrow 挑眉 怀疑 质疑
😐 neutral 面无表情 无语
😑 expressionless 无语 无奈 面无表情
😶 no_mouth 沉默 说不出话
😏 smirk 得意 坏笑 哼
😒 unamused 不爽 不满 白眼
🙄 roll_eyes 翻白眼 无语 无奈
😬 grimacing 尴尬 咧嘴 牙
🤥 lying 撒谎 匹诺曹 说谎
😌 relieved 松口气 安心 释然
😔 pensive 失落 难过 沮丧
😪 sleepy 困 睡觉 累
🤤 drooling 流口水 馋 想吃
😴 sleeping 睡觉 睡 zzz 困
😷 mask 口罩 生病 感冒
🤒 thermometer 发烧 生病 体温
🤕 bandage 受伤 包扎 头
🤢 nauseated 恶心 难受 想吐
🤮 vomiting 呕吐 吐 恶心
🤧 sneeze 打喷嚏 感冒 纸巾
🥵 hot 热 中暑 出汗
🥶 cold 冷 冻 发抖
🥴 woozy 晕 醉 迷糊
😵 dizzy 晕 眼花 转
🤯 exploding_head 爆炸 震惊 头炸
🤠 cowboy 牛仔 帽子 西部
🥳 partying 庆祝 派对 生日 干杯
😎 sunglasses 酷 墨镜 帅
🤓 nerd 书呆子 眼镜 学霸
🧐 monocle 单片眼镜 打量 研究
😕 confused 困惑 不解 皱眉
😟 worried 担心 忧虑
🙁 frown 皱眉 不高兴 难过
😮 open_mouth 惊讶 张嘴 吃惊
😯 hushed 惊讶 呆住
😲 astonished 震惊 吃惊
😳 flushed 脸红 尴尬 害羞
🥺 pleading 可怜 求求 委屈
😢 cry 哭 眼泪 难过
😭 sob 大哭 哭 泪崩
😱 scream 尖叫 害怕 惊恐
😖 confounded 纠结 难受 困惑
😣 persevere 挣扎 忍耐 难受
😞 disappointed 失望 沮丧 难过
😓 sweat 汗 无奈 累
😩 weary 疲惫 累 难受
😫 tired 累 疲倦 崩溃
😤 triumph 得意 生气 哼
😡 rage 生气 愤怒 火大
😠 angry 生气 不爽 愤怒
🤬 cursing 骂人 爆粗 愤怒
😈 devil 恶魔 坏笑 调皮
💀 skull 骷髅 死亡 死
💩 poop 便便 屎 大便
🤡 clown 小丑 可笑 滑稽
👻 ghost 鬼 幽灵 万圣节
👽 alien 外星人 飞碟 宇宙
🤖 robot 机器人 人工智能 bot
😺 smiley_cat 猫 笑 开心
😹 joy_cat 猫 笑哭
😻 heart_eyes_cat 猫 爱 喜欢
😿 crying_cat 猫 哭 难过
🙀 weary_cat 猫 震惊 害怕
""",
    ANIMALS: """
🐶 dog 狗 小狗 汪
🐱 cat 猫 小猫 喵
🐭 mouse 老鼠 耗子
🐹 hamster 仓鼠 可爱
🐰 rabbit 兔子 兔 可爱
🦊 fox 狐狸 聪明
🐻 bear 熊 棕熊
🐼 panda 熊猫 国宝
🐨 koala 考拉 树袋熊
🐯 tiger 老虎 虎
🦁 lion 狮子 王
🐮 cow 牛 奶牛 牛奶
🐷 pig 猪 小猪
🐸 frog 青蛙 蛙
🐵 monkey 猴子 猴
🙈 see_no_evil 蒙眼 不看 猴子
🙉 hear_no_evil 不听 猴子
🙊 speak_no_evil 不说 猴子
🐔 chicken 鸡 小鸡
🐧 penguin 企鹅 南极
🐦 bird 鸟 小鸟
🐤 baby_chick 小鸡 雏鸟
🦆 duck 鸭子 鸭
🦅 eagle 老鹰 鹰
🦉 owl 猫头鹰 睿智
🦇 bat 蝙蝠 夜
🐺 wolf 狼
🐗 boar 野猪 猪
🐴 horse 马
🦄 unicorn 独角兽 彩虹
🐝 bee 蜜蜂 蜜
🐛 bug 虫子 虫 缺陷
🦋 butterfly 蝴蝶 美
🐌 snail 蜗牛 慢
🐞 ladybug 瓢虫 益虫
🐜 ant 蚂蚁 蚁
🕷️ spider 蜘蛛 网
🐢 turtle 乌龟 龟 慢
🐍 snake 蛇 蛇形
🦎 lizard 蜥蜴
🐙 octopus 章鱼 八爪鱼
🦀 crab 螃蟹 蟹
🐠 fish 鱼 热带鱼
🐬 dolphin 海豚 聪明
🐳 whale 鲸鱼 大海
🦈 shark 鲨鱼 凶猛
🐊 crocodile 鳄鱼
🐘 elephant 大象 象
🦒 giraffe 长颈鹿
🐪 camel 骆驼 沙漠
🦓 zebra 斑马
🌵 cactus 仙人掌 沙漠 植物
🌲 tree 树 松树 常青
🌳 deciduous_tree 树 大树
🌴 palm_tree 棕榈 椰子树 海滩
🍀 four_leaf_clover 四叶草 幸运
🌿 herb 草 叶子 植物
🍁 maple_leaf 枫叶 秋天 加拿大
🌻 sunflower 向日葵 花 太阳
🌹 rose 玫瑰 花 爱
🌸 cherry_blossom 樱花 花 春天
🌷 tulip 郁金香 花
🌈 rainbow 彩虹 天气
⭐ star 星星 收藏 标记
🌙 moon 月亮 夜晚
☀️ sun 太阳 晴天 阳光
⛅ cloud 多云 天气 阴
❄️ snowflake 雪花 冷 冬天
⚡ zap 闪电 电 快
🔥 fire 火 热门 燃
💧 droplet 水滴 汗 水
🌊 ocean 海浪 水 大海
""",
    FOOD: """
🍎 apple 苹果 水果
🍐 pear 梨 水果
🍊 orange 橘子 橙子 水果
🍋 lemon 柠檬 酸 水果
🍌 banana 香蕉 水果
🍉 watermelon 西瓜 夏天 水果
🍇 grapes 葡萄 水果
🍓 strawberry 草莓 水果
🍑 peach 桃子 水果
🍒 cherries 樱桃 水果
🥭 mango 芒果 水果
🍍 pineapple 菠萝 水果
🥥 coconut 椰子 水果
🥝 kiwi 猕猴桃 奇异果 水果
🍅 tomato 番茄 西红柿 蔬菜
🍆 eggplant 茄子 蔬菜
🥑 avocado 牛油果 鳄梨
🥦 broccoli 西兰花 蔬菜
🥕 carrot 胡萝卜 蔬菜
🌽 corn 玉米
🌶️ chili 辣椒 辣
🥒 cucumber 黄瓜 蔬菜
🥬 leafy_green 青菜 蔬菜 白菜
🧄 garlic 大蒜 蒜
🧅 onion 洋葱
🥔 potato 土豆 马铃薯
🍠 sweet_potato 红薯 地瓜
🥐 croissant 牛角包 面包 早餐
🥖 baguette 法棍 面包
🍞 bread 面包 吐司
🧀 cheese 奶酪 芝士
🥚 egg 鸡蛋 蛋
🍳 fried_egg 煎蛋 荷包蛋 早餐
🥞 pancakes 煎饼 松饼 早点
🧇 waffle 华夫饼 甜点
🥓 bacon 培根 肉
🍔 burger 汉堡 快餐
🍟 fries 薯条 快餐
🍕 pizza 披萨 比萨
🌭 hotdog 热狗
🥪 sandwich 三明治
🌮 taco 塔可 墨西哥
🌯 burrito 卷饼 墨西哥
🥙 falafel 口袋饼 卷
🍜 ramen 拉面 面条 汤面
🍝 spaghetti 意大利面 面
🍲 stew 汤 炖菜 火锅
🍛 curry 咖喱饭
🍣 sushi 寿司 日本料理
🍱 bento 便当 盒饭
🥟 dumpling 饺子 美食
🍤 fried_shrimp 天妇罗 虾
🍚 rice 米饭 白饭
🍥 fish_cake 鱼板 拉面
🍢 oden 关东煮 串
🍡 dango 团子 甜点
🍦 ice_cream 冰淇淋 雪糕 甜
🍰 cake 蛋糕 生日 甜点
🎂 birthday_cake 生日蛋糕 生日
🍮 custard 布丁 甜点
🍫 chocolate 巧克力 甜 零食
🍬 candy 糖果 甜 零食
🍭 lollipop 棒棒糖 甜
🍯 honey 蜂蜜 甜
🥛 milk 牛奶 饮料
☕ coffee 咖啡 提神 热
🍵 tea 茶 喝茶 绿茶
🍺 beer 啤酒 干杯 喝
🍻 beers 干杯 啤酒 庆祝
🥂 clink 干杯 香槟 庆祝
🍷 wine 红酒 葡萄酒
🍸 cocktail 鸡尾酒 酒吧
🥤 cup_with_straw 饮料 可乐 珍珠
🧊 ice 冰块 冷
""",
    ACTIVITY: """
⚽ soccer 足球 球 运动
🏀 basketball 篮球 运动
🏈 football 橄榄球 美式
⚾ baseball 棒球 运动
🎾 tennis 网球 运动
🏐 volleyball 排球 运动
🏉 rugby 橄榄球
🎱 billiards 台球 撞球 8
🏓 ping_pong 乒乓球 国球
🏸 badminton 羽毛球 运动
🥊 boxing 拳击 手套
🥋 martial_arts 武术 道服 跆拳道
⛳ golf 高尔夫 球
🛹 skateboard 滑板 街头
⛸️ ice_skate 滑冰 溜冰
🎿 ski 滑雪 雪
🚴 biking 骑车 自行车 运动
🏊 swimming 游泳 泳
🏋️ weight_lifting 举重 健身 力量
🤸 cartwheel 侧手翻 体操
🤺 fencing 击剑 剑
🤾 handball 手球
🏇 horse_racing 赛马 骑马
🧘 yoga 瑜伽 冥想 放松
🎯 dart 靶心 目标 命中
🎮 video_game 游戏 手柄 打游戏
🕹️ joystick 摇杆 手柄 游戏
🏆 trophy 奖杯 冠军 赢
🥇 gold 金牌 第一 冠军
🥈 silver 银牌 第二
🥉 bronze 铜牌 第三
🎖️ medal 勋章 奖章
🎫 ticket 票 门票
🎬 clapper 电影 拍板 视频
🎤 microphone 麦克风 唱歌 ktv
🎧 headphones 耳机 听歌 音乐
🎼 musical_score 乐谱 音乐
🎹 piano 钢琴 键盘 音乐
🥁 drum 鼓 打鼓 音乐
🎸 guitar 吉他 音乐 摇滚
🎺 trumpet 小号 乐器
🎻 violin 小提琴 乐器
🎲 game_die 骰子 运气 随机
🃏 joker 扑克 小丑牌
🎳 bowling 保龄球 球
🎪 circus 马戏团 帐篷
🎭 performing_arts 戏剧 面具 表演
🩰 ballet 芭蕾 舞鞋
🎨 art 调色板 画画 设计
🖌️ paintbrush 画笔 画画
✂️ scissors 剪刀 剪 剪切
🎁 gift 礼物 礼物盒 惊喜
🎈 balloon 气球 庆祝 生日
🎉 tada 庆祝 撒花 派对
🎊 confetti 彩带 庆祝
🎆 fireworks 烟花 庆祝 新年
🎇 sparkler 仙女棒 烟花
✨ sparkles 闪光 亮 新
""",
    TRAVEL: """
🚗 car 汽车 车
🚕 taxi 出租车 打车
🚙 suv 车 越野车
🚌 bus 公交车 巴士
🚎 trolleybus 电车 公交
🏎️ racing_car 赛车 快
🚓 police_car 警车 警察
🚑 ambulance 救护车 急救
🚒 fire_engine 消防车 火警
🚐 minibus 面包车 小巴
🛻 pickup 皮卡 货车
🚚 truck 卡车 货车 快递
🚜 tractor 拖拉机 农用
🛵 scooter 踏板车 摩托 外卖
🏍️ motorcycle 摩托车 机车
🚲 bicycle 自行车 单车
🛴 kick_scooter 滑板车 电动滑板
🚂 train 火车 蒸汽
🚄 bullet_train 高铁 动车 快
🚇 metro 地铁 捷运
🚊 tram 有轨电车
🚁 helicopter 直升机 飞机
✈️ airplane 飞机 航班 出行
🛫 airplane_departure 起飞 出发
🛬 airplane_arrival 降落 到达
🚀 rocket 火箭 发射 太空
🛸 flying_saucer 飞碟 外星
🚢 ship 船 轮船
⛵ sailboat 帆船 帆
🛥️ motor_boat 快艇 船
⚓ anchor 锚 港口 停
🗺️ map 地图 世界 路线
🧭 compass 指南针 方向
🏔️ mountain_snow 雪山 高山
⛰️ mountain 山 登山
🌋 volcano 火山 爆发
🏕️ camping 露营 帐篷
🏖️ beach 海滩 度假 沙滩
🏝️ island 海岛 度假
🏜️ desert 沙漠
🏟️ stadium 体育场 球场
🏛️ classical_building 博物馆 建筑
🏰 castle 城堡 欧洲
🗼 tokyo_tower 东京塔 地标
🗽 statue_of_liberty 自由女神 纽约
🏠 house 房子 家 首页
🏢 office 写字楼 公司 办公
🏥 hospital 医院 看病
🏫 school 学校 上课
🏨 hotel 酒店 住宿
⛽ fuel 加油站 加油
🚦 traffic_light 红绿灯 交通
🚧 construction 施工 修路 建设中
🛑 stop_sign 停止 停
🗿 moai 摩艾 复活节岛 石像
🌍 earth 地球 世界 全球
🌞 sun_with_face 晴天 太阳 阳光
🌤️ partly_sunny 晴间多云 天气
🌧️ rain 下雨 雨天 天气
⛈️ thunder 雷雨 暴风雨
🌨️ snow 下雪 雪天
🌪️ tornado 龙卷风 灾难
🌫️ fog 雾 天气 朦胧
""",
    OBJECTS: """
💡 bulb 灯泡 想法 灵感 提示
🔦 flashlight 手电筒 光
🕯️ candle 蜡烛 光 纪念
💻 laptop 电脑 笔记本 工作
🖥️ desktop 台式机 显示器 电脑
⌨️ keyboard 键盘 输入
🖱️ mouse 鼠标 点击
🖨️ printer 打印机 打印
📱 phone 手机 电话 移动
☎️ telephone 电话 座机 打电话
📞 phone_receiver 电话 接听 联系
📟 pager 传呼机 寻呼
📠 fax 传真 传真机
🔋 battery 电池 电量 满格
🔌 plug 插头 充电 电源
📺 tv 电视 电视机
📷 camera 相机 拍照 照片
📹 video_camera 摄像机 录像
🎥 movie_camera 摄影机 电影
📼 videocassette 录像带 复古
⏰ alarm_clock 闹钟 时间 提醒
⌚ watch 手表 时间
⏳ hourglass 沙漏 等待 时间
📡 satellite 卫星天线 信号
🔍 magnifier 放大镜 搜索 查找
🔎 mag_right 放大镜 搜索
🔧 wrench 扳手 维修 设置
🔨 hammer 锤子 敲 工具
⚙️ gear 齿轮 设置 配置 力
🛠️ hammer_and_wrench 工具 修理 维护
🔩 nut_and_bolt 螺丝 螺母 零件
🗜️ clamp 夹具 压缩 压
⚖️ balance_scale 天平 公平 权衡
🔗 link 链接 连接 链条
📎 paperclip 回形针 附件
📌 pushpin 图钉 固定 记录
📁 folder 文件夹 目录
📂 open_folder 文件夹 打开
🗂️ card_index 卡片盒 分类 索引
📅 calendar 日历 日期 计划
📆 calendar_days 日历 日程
📇 card_index_dividers 名片盒 通讯录
📈 chart_up 上涨 增长 数据 报表
📉 chart_down 下跌 亏损 数据
📊 bar_chart 柱状图 统计 数据
📋 clipboard 剪贴板 清单 任务
📝 memo 便签 笔记 写下 记录
✏️ pencil 铅笔 编辑 写
🖊️ pen 笔 签字 写
🖋️ fountain_pen 钢笔 签字
📖 open_book 书 阅读 打开
📚 books 书 学习 图书馆
📓 notebook 笔记本 记录
📔 notebook_decorative 笔记本 日记
📕 closed_book 书 红皮书
📰 newspaper 报纸 新闻 媒体
🔖 bookmark 书签 收藏 标记
🔑 key 钥匙 关键 密码
🗝️ old_key 老钥匙 旧 解锁
🔒 lock 锁 加密 安全
🔓 unlock 解锁 打开 无锁
🔐 closed_lock_with_key 加密 安全 密钥
🛡️ shield 盾牌 保护 安全
🧲 magnet 磁铁 吸引
🧪 test_tube 试管 实验 测试
🔬 microscope 显微镜 研究 科学
🔭 telescope 望远镜 观察 远
💊 pill 药 药片 吃药
💉 syringe 注射 打针 疫苗
🧹 broom 扫帚 打扫 清理
🧺 basket 篮子 洗衣 收纳
🚪 door 门 进出
🪑 chair 椅子 座位
🛏️ bed 床 睡觉 休息
🚿 shower 淋浴 洗澡
🛁 bathtub 浴缸 泡澡
🧼 soap 肥皂 洗手 清洁
🧽 sponge 海绵 擦洗
🗑️ wastebasket 垃圾桶 删除 清理
💰 money_bag 钱袋 有钱 发财
💳 credit_card 信用卡 支付 买单
💎 gem 钻石 宝石 贵重
🎈 balloon 气球
📦 package 包裹 快递 箱子
📮 postbox 邮筒 寄信 邮件
✉️ envelope 信封 邮件 信
""",
    SYMBOLS: """
❤️ heart 心 爱 喜欢 红心
🧡 orange_heart 心 爱
💛 yellow_heart 心 爱
💚 green_heart 心 爱 环保
💙 blue_heart 心 爱
💜 purple_heart 心 爱
🖤 black_heart 心 黑
🤍 white_heart 心 白
💔 broken_heart 心碎 失恋 难过
❣️ heart_exclamation 心 感叹 爱
💕 two_hearts 心 爱 双
💞 revolving_hearts 心 爱
💓 heartbeat 心跳 心 爱
💗 growing_heart 心 爱
💖 sparkling_heart 心 闪 爱
💘 cupid 丘比特 爱 一见钟情
💝 gift_heart 心 礼物
✝️ latin_cross 十字架 基督教
☪️ star_and_crescent 星月 伊斯兰
🕉️ om 唵 印度教
✡️ star_of_david 六芒星 犹太教
☸️ dharma_wheel 法轮 佛教
🔯 six_pointed_star 六角星 占卜
⚛️ atom 原子 科学 物理
♻️ recycle 回收 环保 循环
✅ check 对 完成 通过 是
☑️ ballot_check 勾选 完成 选项
✔️ heavy_check_mark 勾 对 完成
❌ cross_mark 错 叉 失败 否
❎ negative_squared_cross 叉 否
⭕ circle 圈 对 正确
❗ exclamation 感叹 重要 注意
❕ white_exclamation 感叹 注意
❓ question 问号 疑问 什么
❔ white_question 问号 疑问
‼️ double_exclamation 双感叹 重要 紧急
⁉️ interrobang 疑问 震惊 什么
⚠️ warning 警告 注意 危险
🚫 no_entry 禁止 不行 不要
💯 hundred 满分 一百分 完美
🔞 no_one_under_eighteen 18 禁 未成年
♾️ infinity 无限 无穷
🆗 ok 可以 好的 行
🆕 new 新 刚出
🆒 cool 酷 赞
🆓 free 免费 白嫖
🔝 top 顶 置顶 顶部
🔚 end 结束 完
🔛 on 开 启动
🔜 soon 即将 马上
🔄 refresh 刷新 循环 同步
🔁 repeat 循环 重播
🔀 shuffle 随机 打乱 洗牌
▶️ play 播放 开始 运行
⏸️ pause 暂停 停
⏹️ stop 停止 结束
⏭️ next_track 下一个 跳过
🔊 speaker 音量 声音 外放
🔇 mute 静音 关闭声音
🔔 bell 铃 提醒 通知
🔕 bell_slash 静音 免打扰
💬 speech_balloon 消息 聊天 评论
💭 thought_balloon 想法 思考 心里话
🗯️ right_anger_bubble 生气 愤怒 气话
🔹 small_blue_diamond 蓝菱形 列表
🔸 small_orange_diamond 橙菱形 列表
▪️ black_small_square 方块 项目符号
⚡ high_voltage 闪电 快 电
💤 zzz 睡觉 困 睡
💢 anger 生气 愤怒 火
💥 boom 爆炸 冲突 撞
💫 dizzy 头晕 星 闪
💦 sweat_drops 汗 水花 溅
💨 dash 风 跑 快
🕳️ hole 洞 空 坑
🎵 musical_note 音符 音乐
➕ plus 加 添加 新增
➖ minus 减 删除 减少
➗ divide 除 除法
✖️ multiply 乘 乘法 错
♠️ spade 黑桃 扑克
♥️ heart_suit 红桃 扑克
♦️ diamond_suit 方块 扑克
♣️ club_suit 梅花 扑克
™️ trademark 商标 品牌
©️ copyright 版权 著作权
®️ registered 注册商标
⭐ star_favorite 星 收藏 喜欢
🌟 glowing_star 星 亮 闪耀
🌠 shooting_star 流星 许愿
""",
}


# ---------------------------------------------------------------------------
# 解析与查询
# ---------------------------------------------------------------------------
def _parse(block: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """把一段表格文本解析成 ``((表情, (关键词…)), …)``。

    行格式：``表情 关键词 关键词 …``；空行忽略，行内多余空白无关紧要。
    """
    items: list[tuple[str, tuple[str, ...]]] = []
    for line in block.splitlines():
        parts = line.split()
        if not parts:
            continue
        items.append((parts[0], tuple(parts[1:])))
    return tuple(items)


# 分类键 -> 表情表（不含"最近"，那个由面板自己维护）
TABLE: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    key: _parse(block) for key, block in RAW.items()
}
# 旗帜单独来自生成的表（有精灵图，渲染方式也不同）
TABLE[FLAGS] = tuple((char, flag_data.keywords(char)) for char in flag_data.FLAG_CHARS)

# 搜索时的固定遍历顺序：表情 -> 动物 -> 食物 -> 活动 -> 旅行 -> 物品 -> 符号 -> 旗帜
SEARCH_ORDER: tuple[str, ...] = (FACES, ANIMALS, FOOD, ACTIVITY, TRAVEL,
                                 OBJECTS, SYMBOLS, FLAGS)

# 全部表情字符（去重，保持分类顺序）
ALL_EMOJI: tuple[str, ...] = tuple(
    dict.fromkeys(char for key in SEARCH_ORDER for char, _ in TABLE[key]))


def is_flag(char: str) -> bool:
    """这个表情要不要走精灵图（面板据此决定格子画图标还是画文字）。"""
    return flag_data.is_flag(char)


def item_count(key: str) -> int:
    """某个分类的表情数量（"最近"是动态的，永远返回 0）。"""
    return len(TABLE.get(key, ()))


def total_count() -> int:
    return len(ALL_EMOJI)


def items(key: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return TABLE.get(key, ())


def keywords(char: str) -> tuple[str, ...]:
    for key in SEARCH_ORDER:
        for item, words in TABLE[key]:
            if item == char:
                return words
    return ()


def search(keyword: str, *, limit: int = 120) -> list[str]:
    """按关键词搜表情：中英子串匹配，输入的关键词本身是表情时精确命中它。

    空关键词返回空列表（由面板显示"最近"分类，而不是"搜到全部"）。
    """
    query = keyword.strip().lower()
    if not query:
        return []
    exact: list[str] = []
    hits: list[str] = []
    for key in SEARCH_ORDER:
        for char, words in TABLE[key]:
            if query == char:
                exact.append(char)
            elif any(query in word.lower() for word in words):
                hits.append(char)
    return (exact + hits)[:limit]


def label_of(key: str) -> str:
    for cat_key, label, _icon in CATEGORIES:
        if cat_key == key:
            return label
    return key
