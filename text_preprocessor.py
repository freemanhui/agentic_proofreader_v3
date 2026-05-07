import re

def advanced_preprocessing(text: str) -> str:
    if not text:
        return ""

    # 1. 剔除特定的系统杂质（Screen Reader 等无关指令）
    system_noise = [
        r"Turn on screen reader support",
        r"To enable screen reader support, press.*",
        r".*has left the document\.",
        r"AI Proofreading Usage"
    ]
    for pattern in system_noise:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # 2. 深度清洗不可见字符 (包括你例子中的 \u2060, \u200b, \u200c 等)
    # \u200b-\u200f: 零宽空格、连通符等
    # \u2060-\u206f: 词连结符 (Word Joiner) 等不可见格式符
    # \ufeff: BOM
    invisible_pattern = re.compile(r'[\u200b-\u200f\u2060-\u206f\ufeff\u200c\u200d]')
    text = invisible_pattern.sub('', text)

    # 3. 处理过度堆积的空白符和换行
    # 将 3 个及以上的换行符压缩为 2 个（保留段落感，但去除深坑）
    text = re.sub(r'\n{3,}', '\n\n', text)
    # 去除行尾多余的空格
    text = "\n".join([line.strip() for line in text.splitlines()])

    # 4. (可选) 去重逻辑
    # 如果你的业务场景经常出现完全重复的块，可以按段落去重
    paragraphs = text.split('\n\n')
    unique_paragraphs = []
    seen = set()
    for p in paragraphs:
        p_clean = p.strip()
        if p_clean and p_clean not in seen:
            unique_paragraphs.append(p_clean)
            seen.add(p_clean)
    
    return "\n\n".join(unique_paragraphs)