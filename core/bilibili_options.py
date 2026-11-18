"""Small platform metadata contract shared by snapshot validation and UI."""
def validate_bilibili_options(value):
    if type(value.get('copyright')) is not int or value['copyright'] not in (1, 2):
        raise ValueError('请选择B站自制或转载类型')
    source = value.get('source', '')
    if not isinstance(source, str) or len(source) > 2048 or any(ord(c) < 32 for c in source):
        raise ValueError('B站来源文字无效')
    if value['copyright'] == 2 and not source.strip():
        raise ValueError('B站转载投稿需要填写真实来源')
    path = value.get('category_path')
    if not isinstance(path, list) or not 1 <= len(path) <= 3 or any(not isinstance(x, str) or not 1 <= len(x.strip()) <= 40 for x in path):
        raise ValueError('请填写B站分区路径，例如：运动 / 足球')
    if not 1 <= len(value.get('hashtags', [])) <= 10:
        raise ValueError('B站投稿需要1～10个内容相关标签')
    return value
