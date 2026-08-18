# 4丁目のコロッケ屋さん

Hugoを使わない静的サイトです。店舗情報と54商品の価格は
[`data/site.yml`](data/site.yml)を唯一の編集元（SSoT）とし、WebページとA4 1ページの
メニューPDFを同じデータから生成します。Netlifyのビルド成果物は`dist/`です。

## ローカル環境の準備

Python 3.13が必要です。PDFの印刷レイアウトを画像で確認する場合は、Popplerの
`pdftoppm`も必要です。

macOSでは次のように準備できます。

```sh
brew install python@3.13 poppler
```

プロジェクト直下で仮想環境を作成し、依存関係をインストールします。

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## ビルド

Web、sitemap、静的資産、PDFをまとめて生成・検証します。`dist/`は毎回クリーンに
生成されるため、生成済みHTMLやPDFを直接編集しないでください。

```sh
python scripts/build.py
```

PDFだけを更新したい場合は、既存の`dist/`を残したまま次を実行します。

```sh
python scripts/build.py --pdf-only
```

PDFの目的、紙面設計、検証内容、変更時のチェックリストは
[`docs/menu-pdf.md`](docs/menu-pdf.md)を参照してください。

## ローカル表示確認

ビルド後、生成物を静的サーバーで配信します。

```sh
python -m http.server 8000 --directory dist
```

ブラウザで次を開いてください。

- <http://localhost:8000/>（Webページ）
- <http://localhost:8000/menu.pdf>（A4メニューPDF）

PDFの紙面をPNGに変換して確認する場合は、次を実行します。

```sh
mkdir -p tmp/pdf-preview
pdftoppm -png dist/menu.pdf tmp/pdf-preview/menu
```

生成された`tmp/pdf-preview/menu-1.png`で印刷レイアウトを確認します。確認項目は
[`docs/menu-pdf.md`](docs/menu-pdf.md)にまとめています。

## 価格・メニューの更新

価格や商品名を変更するときは、`data/site.yml`の該当商品の`name`、任意の`flyer_name`、
`price`、`unit`、`description`を更新し、`site.revision_date`も改定日に変更します。
PDFの並びを変更するときは、同じファイルの`pdf_grid`で商品IDだけを移動します。
商品名・価格を`templates/`や`dist/`へ複製して編集しないでください。

`flyer_name`はチラシだけ短くしたい商品にだけ指定します。省略した場合は`name`を
チラシ名にも使います。

営業時間は`site.hours`の`start`と`end`だけを更新します。Web、PDF、JSON-LDの
営業時間表示はこの値から生成されます。

店舗情報とJSON-LDのSEO値は`data/site.yml`の`site`配下を編集元とします。

`price.kind`は通常価格が`amount`、加算が`surcharge`、値引きが`discount`です。
未知のID、同じIDの重複、配置からの欠落があるとビルドが止まります。

## Netlify

Netlifyは[`netlify.toml`](netlify.toml)の設定に従い、`python scripts/build.py`を実行して
`dist/`を公開します。ローカルでも同じコマンドを実行してからDeploy Previewを確認して
ください。`/menu.pdf`はブラウザ内表示（`inline`）で、価格改定後に再検証される
キャッシュ設定になっています。

## フォント

PDFにはNoto Sans JP Regular/Boldを同梱し、ビルド時に埋め込みます。フォントはGoogle
Fontsで配布されているNoto Sans JPをウェイト固定したもので、ライセンス本文は
[`fonts/NotoSansJP-OFL.txt`](fonts/NotoSansJP-OFL.txt)にあります。PDF専用フォントは
Web配信物`dist/`へはコピーされません。
