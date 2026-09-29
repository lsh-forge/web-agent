import re


def parse_camel_to_snake(camel_str: str) -> str:
    """
    支持整句清洗：保留文本结构，仅将驼峰/不规则下划线命名转为标准snake_case
    核心：处理整句中的每个命名片段，保留参数名/值的上下文关系
    """
    if not isinstance(camel_str, str):
        raise TypeError("输入必须是字符串类型")
    if not camel_str:
        return ""

    # 定义单个命名的清洗逻辑（抽离为内部函数）
    def clean_single_name(name: str) -> str:
        # 步骤1：驼峰转下划线（原逻辑）
        snake_name = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
        # 步骤2：处理连续大写（原逻辑）
        snake_name = re.sub(r'([A-Z]+)([A-Z][a-z])',
                           lambda m: f"{m.group(1).lower()}_{m.group(2).lower()}",
                           snake_name)
        # 步骤3：清理下划线（合并连续+移除首尾）
        snake_name = re.sub(r'_+', '_', snake_name)
        snake_name = snake_name.strip('_')
        return snake_name

    # 核心：按空格分割整句，逐个清洗命名片段，再拼接还原结构
    parts = camel_str.split()
    cleaned_parts = [clean_single_name(part) for part in parts]
    return ' '.join(cleaned_parts)


