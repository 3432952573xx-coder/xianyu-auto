#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xianyu_auto.adapters.netdisk import ManualShareStore
from xianyu_auto.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="录入百度网盘人工分享结果")
    parser.add_argument(
        "--share-key",
        help="商品稳定目录名；留空时从待处理列表选择",
    )
    args = parser.parse_args()

    settings = Settings.load()
    store = ManualShareStore(settings.netdisk_manual_file)
    share_key = (args.share_key or "").strip()
    if not share_key:
        pending = store.pending()
        if not pending:
            raise SystemExit("没有待人工分享的目录；请先上传或传入 --share-key")
        names = list(pending)
        print("待手动分享文件：")
        for index, name in enumerate(names, start=1):
            print(f"{index}. {name} -> {pending[name].get('remote_path', '')}")
        choice = input("输入序号: ").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(names):
            raise SystemExit("序号无效")
        share_key = names[int(choice) - 1]

    print("请输入百度网盘手动分享结果；内容不会回显，只保存在本机权限为 0600 的映射文件。")
    link = getpass.getpass("分享链接: ").strip()
    code = getpass.getpass("提取码（4 位）: ").strip()
    if not link.startswith(("https://pan.baidu.com/", "http://pan.baidu.com/")):
        raise SystemExit("分享链接格式不正确")
    if len(code) != 4 or not code.isalnum():
        raise SystemExit("提取码应为 4 位字母或数字")
    store.save_share(share_key, link=link, extract_code=code)
    print(f"已保存 {share_key} 的分享结果。")
    print("下次 worker 运行时会把该链接绑定到对应商品，不会重新发布商品。")


if __name__ == "__main__":
    main()
