"""旗帜数据表 + 精灵图坐标（**零 Qt 依赖**，可脱离界面单测）。

这个文件由 ``tools/build_flags.py`` 生成，别手改 —— 改了就再跑一次脚本。
（表内容变化时脚本会重下素材；下载缓存放在仓库根的 .flag_cache/。）

    flags.py    本文件：字符、国家代码、中英关键词、精灵图坐标
    assets/flags.png   一张 16 列 × 17 行的精灵图，每格 64×64（面板用）
    assets/flags/*.png 逐张独立 PNG，000.png 起按槽位编号（消息文本 <img> 用）

两种形态的原因：面板格子是 QToolButton 图标，精灵图一次读盘最省；而消息文本/输入框是
QTextDocument 渲染的，`<img src>` 只能按 URL 取单个文件，喂不了精灵图里的一格。

数据来源与许可
--------------
* 旗图：Twemoji（jdecked/twemoji），CC-BY 4.0 —— 署名见 mchat/assets/README.md
* 国家名：Unicode CLDR localenames/territories（zh / en），Unicode License

为什么用精灵图而不是 Unicode 国旗字符
------------------------------------
Windows 的 Segoe UI Emoji 不画国旗，只会渲染成两个字母（"CN"）；英格兰/苏格兰/威尔士
那种 tag 序列更是几乎没字体支持。所以走图片方案：字符只当"查槽位用的 key"
（Telegram 也是这么做的，见 lib_ui 的 emoji.txt + emoji_*.webp）。
"""
from __future__ import annotations

import os
import sys
from urllib.parse import quote

COLS = 16
CELL = 64
SPRITE_NAME = "flags.png"
PNG_DIRNAME = "flags"

# (字符, 国家代码, 关键词串) —— 顺序就是精灵图里的槽位顺序
# 关键词串用 "|" 分隔（不是空格）：这样 "united states" 这种多词短语能原样保留
FLAGS: tuple[tuple[str, str, str], ...] = (
    ("🇦🇨", "ac", "ac|阿森松岛|ascension island|ascension|island"),
    ("🇦🇩", "ad", "ad|安道尔|andorra"),
    ("🇦🇪", "ae", "ae|阿拉伯联合酋长国|united arab emirates|united|arab|emirates"),
    ("🇦🇫", "af", "af|阿富汗|afghanistan"),
    ("🇦🇬", "ag", "ag|安提瓜和巴布达|antigua & barbuda|antigua|barbuda"),
    ("🇦🇮", "ai", "ai|安圭拉|anguilla"),
    ("🇦🇱", "al", "al|阿尔巴尼亚|albania"),
    ("🇦🇲", "am", "am|亚美尼亚|armenia"),
    ("🇦🇴", "ao", "ao|安哥拉|angola"),
    ("🇦🇶", "aq", "aq|南极洲|antarctica"),
    ("🇦🇷", "ar", "ar|阿根廷|argentina"),
    ("🇦🇸", "as", "as|美属萨摩亚|american samoa|american|samoa"),
    ("🇦🇹", "at", "at|奥地利|austria"),
    ("🇦🇺", "au", "au|澳大利亚|australia"),
    ("🇦🇼", "aw", "aw|阿鲁巴|aruba"),
    ("🇦🇽", "ax", "ax|奥兰群岛|åland islands|aland islands|åland|aland|islands"),
    ("🇦🇿", "az", "az|阿塞拜疆|azerbaijan"),
    ("🇧🇦", "ba", "ba|波斯尼亚和黑塞哥维那|bosnia & herzegovina|bosnia|herzegovina"),
    ("🇧🇧", "bb", "bb|巴巴多斯|barbados"),
    ("🇧🇩", "bd", "bd|孟加拉国|bangladesh"),
    ("🇧🇪", "be", "be|比利时|belgium"),
    ("🇧🇫", "bf", "bf|布基纳法索|burkina faso|burkina|faso"),
    ("🇧🇬", "bg", "bg|保加利亚|bulgaria"),
    ("🇧🇭", "bh", "bh|巴林|bahrain"),
    ("🇧🇮", "bi", "bi|布隆迪|burundi"),
    ("🇧🇯", "bj", "bj|贝宁|benin"),
    ("🇧🇱", "bl", "bl|圣巴泰勒米|saint barthélemy|saint barthelemy|saint|barthélemy|barthelemy"),
    ("🇧🇲", "bm", "bm|百慕大|bermuda"),
    ("🇧🇳", "bn", "bn|文莱|brunei"),
    ("🇧🇴", "bo", "bo|玻利维亚|bolivia"),
    ("🇧🇶", "bq", "bq|荷属加勒比区|caribbean netherlands|caribbean|netherlands"),
    ("🇧🇷", "br", "br|巴西|brazil"),
    ("🇧🇸", "bs", "bs|巴哈马|bahamas"),
    ("🇧🇹", "bt", "bt|不丹|bhutan"),
    ("🇧🇻", "bv", "bv|布韦岛|bouvet island|bouvet|island"),
    ("🇧🇼", "bw", "bw|博茨瓦纳|botswana"),
    ("🇧🇾", "by", "by|白俄罗斯|belarus"),
    ("🇧🇿", "bz", "bz|伯利兹|belize"),
    ("🇨🇦", "ca", "ca|加拿大|canada"),
    ("🇨🇨", "cc", "cc|科科斯 基林 群岛|科科斯|基林|群岛|cocos keeling islands|cocos|keeling|islands"),
    ("🇨🇩", "cd", "cd|刚果 金|刚果|金|congo - kinshasa|congo|kinshasa"),
    ("🇨🇫", "cf", "cf|中非共和国|central african republic|central|african|republic"),
    ("🇨🇬", "cg", "cg|刚果 布|刚果|布|congo - brazzaville|congo|brazzaville"),
    ("🇨🇭", "ch", "ch|瑞士|switzerland"),
    ("🇨🇮", "ci", "ci|科特迪瓦|côte d’ivoire|cote d'ivoire|côte|cote|d’ivoire|d'ivoire"),
    ("🇨🇰", "ck", "ck|库克群岛|cook islands|cook|islands"),
    ("🇨🇱", "cl", "cl|智利|chile"),
    ("🇨🇲", "cm", "cm|喀麦隆|cameroon"),
    ("🇨🇳", "cn", "cn|中国|china"),
    ("🇨🇴", "co", "co|哥伦比亚|colombia"),
    ("🇨🇵", "cp", "cp|克利珀顿岛|clipperton island|clipperton|island"),
    ("🇨🇶", "cq", "cq|萨克岛|sark"),
    ("🇨🇷", "cr", "cr|哥斯达黎加|costa rica|costa|rica"),
    ("🇨🇺", "cu", "cu|古巴|cuba"),
    ("🇨🇻", "cv", "cv|佛得角|cape verde|cape|verde"),
    ("🇨🇼", "cw", "cw|库拉索|curaçao|curacao"),
    ("🇨🇽", "cx", "cx|圣诞岛|christmas island|christmas|island"),
    ("🇨🇾", "cy", "cy|塞浦路斯|cyprus"),
    ("🇨🇿", "cz", "cz|捷克|czechia"),
    ("🇩🇪", "de", "de|德国|germany"),
    ("🇩🇬", "dg", "dg|迪戈加西亚岛|diego garcia|diego|garcia"),
    ("🇩🇯", "dj", "dj|吉布提|djibouti"),
    ("🇩🇰", "dk", "dk|丹麦|denmark"),
    ("🇩🇲", "dm", "dm|多米尼克|dominica"),
    ("🇩🇴", "do", "do|多米尼加共和国|dominican republic|dominican|republic"),
    ("🇩🇿", "dz", "dz|阿尔及利亚|algeria"),
    ("🇪🇦", "ea", "ea|休达及梅利利亚|ceuta & melilla|ceuta|melilla"),
    ("🇪🇨", "ec", "ec|厄瓜多尔|ecuador"),
    ("🇪🇪", "ee", "ee|爱沙尼亚|estonia"),
    ("🇪🇬", "eg", "eg|埃及|egypt"),
    ("🇪🇭", "eh", "eh|西撒哈拉|western sahara|western|sahara"),
    ("🇪🇷", "er", "er|厄立特里亚|eritrea"),
    ("🇪🇸", "es", "es|西班牙|spain"),
    ("🇪🇹", "et", "et|埃塞俄比亚|ethiopia"),
    ("🇪🇺", "eu", "eu|欧盟|european union|european|union"),
    ("🇫🇮", "fi", "fi|芬兰|finland"),
    ("🇫🇯", "fj", "fj|斐济|fiji"),
    ("🇫🇰", "fk", "fk|福克兰群岛|falkland islands|falkland|islands"),
    ("🇫🇲", "fm", "fm|密克罗尼西亚|micronesia"),
    ("🇫🇴", "fo", "fo|法罗群岛|faroe islands|faroe|islands"),
    ("🇫🇷", "fr", "fr|法国|france"),
    ("🇬🇦", "ga", "ga|加蓬|gabon"),
    ("🇬🇧", "gb", "gb|英国|united kingdom|united|kingdom"),
    ("🇬🇩", "gd", "gd|格林纳达|grenada"),
    ("🇬🇪", "ge", "ge|格鲁吉亚|georgia"),
    ("🇬🇫", "gf", "gf|法属圭亚那|french guiana|french|guiana"),
    ("🇬🇬", "gg", "gg|根西岛|guernsey"),
    ("🇬🇭", "gh", "gh|加纳|ghana"),
    ("🇬🇮", "gi", "gi|直布罗陀|gibraltar"),
    ("🇬🇱", "gl", "gl|格陵兰|greenland"),
    ("🇬🇲", "gm", "gm|冈比亚|gambia"),
    ("🇬🇳", "gn", "gn|几内亚|guinea"),
    ("🇬🇵", "gp", "gp|瓜德罗普|guadeloupe"),
    ("🇬🇶", "gq", "gq|赤道几内亚|equatorial guinea|equatorial|guinea"),
    ("🇬🇷", "gr", "gr|希腊|greece"),
    ("🇬🇸", "gs", "gs|南乔治亚和南桑威奇群岛|south georgia & south sandwich islands|south|georgia|sandwich|islands"),
    ("🇬🇹", "gt", "gt|危地马拉|guatemala"),
    ("🇬🇺", "gu", "gu|关岛|guam"),
    ("🇬🇼", "gw", "gw|几内亚比绍|guinea-bissau|guinea|bissau"),
    ("🇬🇾", "gy", "gy|圭亚那|guyana"),
    ("🇭🇰", "hk", "hk|中国香港特别行政区|hong kong sar china|hong|kong|sar|china"),
    ("🇭🇲", "hm", "hm|赫德岛和麦克唐纳群岛|heard & mcdonald islands|heard|mcdonald|islands"),
    ("🇭🇳", "hn", "hn|洪都拉斯|honduras"),
    ("🇭🇷", "hr", "hr|克罗地亚|croatia"),
    ("🇭🇹", "ht", "ht|海地|haiti"),
    ("🇭🇺", "hu", "hu|匈牙利|hungary"),
    ("🇮🇨", "ic", "ic|加纳利群岛|canary islands|canary|islands"),
    ("🇮🇩", "id", "id|印度尼西亚|indonesia"),
    ("🇮🇪", "ie", "ie|爱尔兰|ireland"),
    ("🇮🇱", "il", "il|以色列|israel"),
    ("🇮🇲", "im", "im|马恩岛|isle of man|isle|man"),
    ("🇮🇳", "in", "in|印度|india"),
    ("🇮🇴", "io", "io|英属印度洋领地|british indian ocean territory|british|indian|ocean|territory"),
    ("🇮🇶", "iq", "iq|伊拉克|iraq"),
    ("🇮🇷", "ir", "ir|伊朗|iran"),
    ("🇮🇸", "is", "is|冰岛|iceland"),
    ("🇮🇹", "it", "it|意大利|italy"),
    ("🇯🇪", "je", "je|泽西岛|jersey"),
    ("🇯🇲", "jm", "jm|牙买加|jamaica"),
    ("🇯🇴", "jo", "jo|约旦|jordan"),
    ("🇯🇵", "jp", "jp|日本|japan"),
    ("🇰🇪", "ke", "ke|肯尼亚|kenya"),
    ("🇰🇬", "kg", "kg|吉尔吉斯斯坦|kyrgyzstan"),
    ("🇰🇭", "kh", "kh|柬埔寨|cambodia"),
    ("🇰🇮", "ki", "ki|基里巴斯|kiribati"),
    ("🇰🇲", "km", "km|科摩罗|comoros"),
    ("🇰🇳", "kn", "kn|圣基茨和尼维斯|saint kitts & nevis|saint|kitts|nevis"),
    ("🇰🇵", "kp", "kp|朝鲜|north korea|north|korea"),
    ("🇰🇷", "kr", "kr|韩国|south korea|south|korea"),
    ("🇰🇼", "kw", "kw|科威特|kuwait"),
    ("🇰🇾", "ky", "ky|开曼群岛|cayman islands|cayman|islands"),
    ("🇰🇿", "kz", "kz|哈萨克斯坦|kazakhstan"),
    ("🇱🇦", "la", "la|老挝|laos"),
    ("🇱🇧", "lb", "lb|黎巴嫩|lebanon"),
    ("🇱🇨", "lc", "lc|圣卢西亚|saint lucia|saint|lucia"),
    ("🇱🇮", "li", "li|列支敦士登|liechtenstein"),
    ("🇱🇰", "lk", "lk|斯里兰卡|sri lanka|sri|lanka"),
    ("🇱🇷", "lr", "lr|利比里亚|liberia"),
    ("🇱🇸", "ls", "ls|莱索托|lesotho"),
    ("🇱🇹", "lt", "lt|立陶宛|lithuania"),
    ("🇱🇺", "lu", "lu|卢森堡|luxembourg"),
    ("🇱🇻", "lv", "lv|拉脱维亚|latvia"),
    ("🇱🇾", "ly", "ly|利比亚|libya"),
    ("🇲🇦", "ma", "ma|摩洛哥|morocco"),
    ("🇲🇨", "mc", "mc|摩纳哥|monaco"),
    ("🇲🇩", "md", "md|摩尔多瓦|moldova"),
    ("🇲🇪", "me", "me|黑山|montenegro"),
    ("🇲🇫", "mf", "mf|法属圣马丁|saint martin|saint|martin"),
    ("🇲🇬", "mg", "mg|马达加斯加|madagascar"),
    ("🇲🇭", "mh", "mh|马绍尔群岛|marshall islands|marshall|islands"),
    ("🇲🇰", "mk", "mk|北马其顿|north macedonia|north|macedonia"),
    ("🇲🇱", "ml", "ml|马里|mali"),
    ("🇲🇲", "mm", "mm|缅甸|myanmar burma|myanmar|burma"),
    ("🇲🇳", "mn", "mn|蒙古|mongolia"),
    ("🇲🇴", "mo", "mo|中国澳门特别行政区|macao sar china|macao|sar|china"),
    ("🇲🇵", "mp", "mp|北马里亚纳群岛|northern mariana islands|northern|mariana|islands"),
    ("🇲🇶", "mq", "mq|马提尼克|martinique"),
    ("🇲🇷", "mr", "mr|毛里塔尼亚|mauritania"),
    ("🇲🇸", "ms", "ms|蒙特塞拉特|montserrat"),
    ("🇲🇹", "mt", "mt|马耳他|malta"),
    ("🇲🇺", "mu", "mu|毛里求斯|mauritius"),
    ("🇲🇻", "mv", "mv|马尔代夫|maldives"),
    ("🇲🇼", "mw", "mw|马拉维|malawi"),
    ("🇲🇽", "mx", "mx|墨西哥|mexico"),
    ("🇲🇾", "my", "my|马来西亚|malaysia"),
    ("🇲🇿", "mz", "mz|莫桑比克|mozambique"),
    ("🇳🇦", "na", "na|纳米比亚|namibia"),
    ("🇳🇨", "nc", "nc|新喀里多尼亚|new caledonia|new|caledonia"),
    ("🇳🇪", "ne", "ne|尼日尔|niger"),
    ("🇳🇫", "nf", "nf|诺福克岛|norfolk island|norfolk|island"),
    ("🇳🇬", "ng", "ng|尼日利亚|nigeria"),
    ("🇳🇮", "ni", "ni|尼加拉瓜|nicaragua"),
    ("🇳🇱", "nl", "nl|荷兰|netherlands"),
    ("🇳🇴", "no", "no|挪威|norway"),
    ("🇳🇵", "np", "np|尼泊尔|nepal"),
    ("🇳🇷", "nr", "nr|瑙鲁|nauru"),
    ("🇳🇺", "nu", "nu|纽埃|niue"),
    ("🇳🇿", "nz", "nz|新西兰|new zealand|new|zealand"),
    ("🇴🇲", "om", "om|阿曼|oman"),
    ("🇵🇦", "pa", "pa|巴拿马|panama"),
    ("🇵🇪", "pe", "pe|秘鲁|peru"),
    ("🇵🇫", "pf", "pf|法属波利尼西亚|french polynesia|french|polynesia"),
    ("🇵🇬", "pg", "pg|巴布亚新几内亚|papua new guinea|papua|new|guinea"),
    ("🇵🇭", "ph", "ph|菲律宾|philippines"),
    ("🇵🇰", "pk", "pk|巴基斯坦|pakistan"),
    ("🇵🇱", "pl", "pl|波兰|poland"),
    ("🇵🇲", "pm", "pm|圣皮埃尔和密克隆群岛|saint pierre & miquelon|saint|pierre|miquelon"),
    ("🇵🇳", "pn", "pn|皮特凯恩群岛|pitcairn islands|pitcairn|islands"),
    ("🇵🇷", "pr", "pr|波多黎各|puerto rico|puerto|rico"),
    ("🇵🇸", "ps", "ps|巴勒斯坦领土|palestinian territories|palestinian|territories"),
    ("🇵🇹", "pt", "pt|葡萄牙|portugal"),
    ("🇵🇼", "pw", "pw|帕劳|palau"),
    ("🇵🇾", "py", "py|巴拉圭|paraguay"),
    ("🇶🇦", "qa", "qa|卡塔尔|qatar"),
    ("🇷🇪", "re", "re|留尼汪|réunion|reunion"),
    ("🇷🇴", "ro", "ro|罗马尼亚|romania"),
    ("🇷🇸", "rs", "rs|塞尔维亚|serbia"),
    ("🇷🇺", "ru", "ru|俄罗斯|russia"),
    ("🇷🇼", "rw", "rw|卢旺达|rwanda"),
    ("🇸🇦", "sa", "sa|沙特阿拉伯|saudi arabia|saudi|arabia"),
    ("🇸🇧", "sb", "sb|所罗门群岛|solomon islands|solomon|islands"),
    ("🇸🇨", "sc", "sc|塞舌尔|seychelles"),
    ("🇸🇩", "sd", "sd|苏丹|sudan"),
    ("🇸🇪", "se", "se|瑞典|sweden"),
    ("🇸🇬", "sg", "sg|新加坡|singapore"),
    ("🇸🇭", "sh", "sh|圣赫勒拿|saint helena|saint|helena"),
    ("🇸🇮", "si", "si|斯洛文尼亚|slovenia"),
    ("🇸🇯", "sj", "sj|斯瓦尔巴和扬马延|svalbard & jan mayen|svalbard|jan|mayen"),
    ("🇸🇰", "sk", "sk|斯洛伐克|slovakia"),
    ("🇸🇱", "sl", "sl|塞拉利昂|sierra leone|sierra|leone"),
    ("🇸🇲", "sm", "sm|圣马力诺|san marino|san|marino"),
    ("🇸🇳", "sn", "sn|塞内加尔|senegal"),
    ("🇸🇴", "so", "so|索马里|somalia"),
    ("🇸🇷", "sr", "sr|苏里南|suriname"),
    ("🇸🇸", "ss", "ss|南苏丹|south sudan|south|sudan"),
    ("🇸🇹", "st", "st|圣多美和普林西比|são tomé & príncipe|sao tome & principe|são|sao|tomé|tome|príncipe|principe"),
    ("🇸🇻", "sv", "sv|萨尔瓦多|el salvador|salvador"),
    ("🇸🇽", "sx", "sx|荷属圣马丁|sint maarten|sint|maarten"),
    ("🇸🇾", "sy", "sy|叙利亚|syria"),
    ("🇸🇿", "sz", "sz|斯威士兰|eswatini"),
    ("🇹🇦", "ta", "ta|特里斯坦-达库尼亚群岛|特里斯坦|达库尼亚群岛|tristan da cunha|tristan|cunha"),
    ("🇹🇨", "tc", "tc|特克斯和凯科斯群岛|turks & caicos islands|turks|caicos|islands"),
    ("🇹🇩", "td", "td|乍得|chad"),
    ("🇹🇫", "tf", "tf|法属南部领地|french southern territories|french|southern|territories"),
    ("🇹🇬", "tg", "tg|多哥|togo"),
    ("🇹🇭", "th", "th|泰国|thailand"),
    ("🇹🇯", "tj", "tj|塔吉克斯坦|tajikistan"),
    ("🇹🇰", "tk", "tk|托克劳|tokelau"),
    ("🇹🇱", "tl", "tl|东帝汶|timor-leste|timor|leste"),
    ("🇹🇲", "tm", "tm|土库曼斯坦|turkmenistan"),
    ("🇹🇳", "tn", "tn|突尼斯|tunisia"),
    ("🇹🇴", "to", "to|汤加|tonga"),
    ("🇹🇷", "tr", "tr|土耳其|türkiye|turkiye"),
    ("🇹🇹", "tt", "tt|特立尼达和多巴哥|trinidad & tobago|trinidad|tobago"),
    ("🇹🇻", "tv", "tv|图瓦卢|tuvalu"),
    ("🇹🇼", "tw", "tw|台湾|taiwan"),
    ("🇹🇿", "tz", "tz|坦桑尼亚|tanzania"),
    ("🇺🇦", "ua", "ua|乌克兰|ukraine"),
    ("🇺🇬", "ug", "ug|乌干达|uganda"),
    ("🇺🇲", "um", "um|美国本土外小岛屿|u.s. outlying islands|outlying|islands"),
    ("🇺🇳", "un", "un|联合国|united nations|united|nations"),
    ("🇺🇸", "us", "us|美国|united states|united|states"),
    ("🇺🇾", "uy", "uy|乌拉圭|uruguay"),
    ("🇺🇿", "uz", "uz|乌兹别克斯坦|uzbekistan"),
    ("🇻🇦", "va", "va|梵蒂冈|vatican city|vatican|city"),
    ("🇻🇨", "vc", "vc|圣文森特和格林纳丁斯|saint vincent & grenadines|saint|vincent|grenadines"),
    ("🇻🇪", "ve", "ve|委内瑞拉|venezuela"),
    ("🇻🇬", "vg", "vg|英属维尔京群岛|british virgin islands|british|virgin|islands"),
    ("🇻🇮", "vi", "vi|美属维尔京群岛|u.s. virgin islands|virgin|islands"),
    ("🇻🇳", "vn", "vn|越南|vietnam"),
    ("🇻🇺", "vu", "vu|瓦努阿图|vanuatu"),
    ("🇼🇫", "wf", "wf|瓦利斯和富图纳|wallis & futuna|wallis|futuna"),
    ("🇼🇸", "ws", "ws|萨摩亚|samoa"),
    ("🇽🇰", "xk", "xk|科索沃|kosovo"),
    ("🇾🇪", "ye", "ye|也门|yemen"),
    ("🇾🇹", "yt", "yt|马约特|mayotte"),
    ("🇿🇦", "za", "za|南非|south africa|south|africa"),
    ("🇿🇲", "zm", "zm|赞比亚|zambia"),
    ("🇿🇼", "zw", "zw|津巴布韦|zimbabwe"),
    ("🏳️", "", "white|flag|白旗"),
    ("🏴", "", "black|flag|黑旗"),
    ("🏁", "", "chequered|flag|格子旗|终点"),
    ("🚩", "", "triangular|flag|三角旗|红旗"),
    ("🏳️‍🌈", "", "rainbow|flag|彩虹旗|lgbt|pride"),
    ("🏳️‍⚧️", "", "transgender|flag|跨性别旗"),
    ("🏴‍☠️", "", "pirate|flag|海盗旗|skull"),
    ("🏴󠁧󠁢󠁥󠁮󠁧󠁿", "", "england|英格兰"),
    ("🏴󠁧󠁢󠁳󠁣󠁴󠁿", "", "scotland|苏格兰"),
    ("🏴󠁧󠁢󠁷󠁬󠁳󠁿", "", "wales|威尔士"),
)

FLAG_CHARS: tuple[str, ...] = tuple(char for char, _code, _words in FLAGS)
INDEX: dict[str, int] = {char: i for i, (char, _c, _w) in enumerate(FLAGS)}


def sprite_path() -> str:
    """精灵图的绝对路径（放在 mchat/assets/ 下，随包分发）。"""
    return os.path.join(_assets_dir(), SPRITE_NAME)


def sprite_exists() -> bool:
    return os.path.isfile(sprite_path())


def _assets_dir() -> str:
    """素材目录：**打包（PyInstaller）后先看解包目录**，源码运行时才是旁边的 mchat/assets/。

    冻结后 ``__file__`` 指向 exe 内部的虚拟路径，按源码树算出来的目录是空的 ——
    于是 ``sprite_exists()/png_exists()`` 全假、旗帜退化成 "CN" 两个字母
    （典型的"开发时好好的，打包就没了"）。PyInstaller 把 datas 解到 ``sys._MEIPASS``，
    onefile 是临时目录、onedir 就是 exe 旁边，里面的层级与源码一致。
    """
    frozen_root = getattr(sys, "_MEIPASS", "")
    bundled = os.path.join(frozen_root, "mchat", "assets") if frozen_root else ""
    if bundled and os.path.isdir(bundled):
        return bundled
    # 兜底分支必须给**绝对路径**：相对路径会被拼进 file:/// URL 里，
    # 变成 "file:///mchat/assets/…"（根目录），富文本里的旗帜就全成了破图标
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets")


def png_dir() -> str:
    """逐张 PNG 的目录（mchat/assets/flags/）。"""
    return os.path.join(_assets_dir(), PNG_DIRNAME)


def png_path(char: str) -> str:
    """第 ``INDEX[char]`` 面旗的独立 PNG 路径（文件名就是槽位序号）。"""
    index = INDEX.get(char)
    return "" if index is None else os.path.join(png_dir(), f"{index:03d}.png")


def png_url(char: str) -> str:
    """给富文本 `<img src="…">` 用的 file:// URL（路径里的空格、引号都要转义）。"""
    path = png_path(char)
    if not path:
        return ""
    # 冒号要留在外面：百分号编码后的 "E%3A" 虽然也能被 QUrl 解回来，但没必要冒险
    return "file:///" + quote(path.replace(os.sep, "/").lstrip("/"), safe="/:")


def char_for_url(url: str) -> str:
    """反查：从 `<img src>` 的 URL 找回旗帜字符（输入框取文本时靠它还原）。"""
    stem = url.rsplit("/", 1)[-1].split(".")[0]
    if stem.isdigit() and int(stem) < len(FLAGS):
        return FLAGS[int(stem)][0]
    return ""


def png_exists(char: str) -> bool:
    path = png_path(char)
    return bool(path) and os.path.isfile(path)


def cell_rect(index: int) -> tuple[int, int, int, int]:
    """第 ``index`` 个旗子在精灵图里的 ``(x, y, w, h)``。"""
    return (index % COLS) * CELL, (index // COLS) * CELL, CELL, CELL


def is_flag(char: str) -> bool:
    return char in INDEX


SEPARATOR = "|"          # 关键词串的分隔符：多词短语（如 united states）原样保留


def keywords(char: str) -> tuple[str, ...]:
    index = INDEX.get(char)
    if index is None:
        return ()
    return tuple(w for w in FLAGS[index][2].split(SEPARATOR) if w)


def code_of(char: str) -> str:
    index = INDEX.get(char)
    return FLAGS[index][1] if index is not None else ""


def search(keyword: str, *, limit: int = 120) -> list[str]:
    query = keyword.strip().lower()
    if not query:
        return []
    hits = []
    for char, code, words in FLAGS:
        if (query == char or query == code
                or any(query in w for w in words.split(SEPARATOR))):
            hits.append(char)
    return hits[:limit]
