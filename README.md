# SSM Historical Airphoto Bridge v1.0

国土地理院の「空中写真（1936年〜1942年頃）」`ort_riku10` を、**一度に1〜2セルだけ**公式XYZタイルから取得し、raw tile・SHA-256・失敗理由・セル別モザイク・manifestをGitHub上に残すための実行パッケージです。

これは、動的Web地図を画面操作する仕組みではありません。公式に公開されたXYZ画像タイルを、GitHub Actionsという外部実行環境から取得する「取得中継」です。したがって、ChatGPTの閲覧環境で地理院地図のJavaScriptキャンバスが読めない問題を回避できます。

## このパッケージがすること

1. 1実行あたり1〜2セル、最大24タイルに制限。
2. 公式 `ort_riku10` タイルを取得し、raw PNGをそのまま保存。
3. タイルごとにURL、HTTP状態、取得日時、SHA-256、失敗理由を台帳化。
4. **必要なタイルが全て揃ったセルだけ**をモザイクPNG化。
5. `data/manifests/manifest.json` を更新。
6. 生成物を同じGitHubリポジトリへコミット。

## しないこと

- 画像から候補を自動判定しない。
- 住宅・物置・工場などを機械的に分類しない。
- B001の範囲を歴史的に確定しない。
- 別年次画像で欠損を埋めない。

画像の候補抽出は、取得済みモザイクをChatGPTが参照できる状態になった後に、セルごとに別台帳として実施します。

## 重要: B001範囲の扱い

`config/SSM_B001_grid_plan.csv` の12セルは、これまでの作業で作られた**暫定範囲**を引き継いでいます。`scope_status=provisional_scope_needs_geographic_verification` としており、この取得パイプラインは範囲の歴史的正当性を保証しません。

## 最初の一度だけ行う設定

1. GitHubで **Public** リポジトリを新規作成する。例: `ssm-historical-airphoto`。
2. このZIPを展開し、中身をリポジトリ直下へアップロードして `main` にコミットする。
3. リポジトリの **Settings → Actions → General → Workflow permissions** で **Read and write permissions** を有効化する。
4. **Actions** タブで `Acquire historical airphoto batch` を開く。
5. 最初は次の小さなバッチだけを実行する。
   - `run_id`: `SSM-T01A`
   - `grid_ids`: `SSM-B001-G01,SSM-B001-G04`
   - `max_new_tiles`: `18`
   - `force_redownload`: `false`

成功後、取得結果は次に保存されます。

```text
data/manifests/manifest.json
data/manifests/tile_ledger.csv
data/manifests/runs/SSM-T01A.json
data/mosaics/ort_riku10/SSM-B001-G01_z18.png
data/mosaics/ort_riku10/SSM-B001-G04_z18.png
```

## ChatGPTへ渡すもの

一度目の実行後に、この形式のURLだけをこの会話へ貼る。

```text
https://raw.githubusercontent.com/<GitHubユーザー名>/<リポジトリ名>/main/data/manifests/manifest.json
```

リポジトリがPublicなら、以後はスクリーンショットを渡さずに、ChatGPTがmanifestとモザイクURLを参照して、セル単位の `visual-lead` 台帳を後続作業へ統合できます。

## 実行順

`config/run_order.csv` に、T01A〜T01Fの順番を固定しています。1回が失敗・タイムアウトしても、既存raw tileと台帳を残したまま次回はキャッシュ再利用します。

## 出典

出典：国土地理院「地理院タイル（空中写真（1936年〜1942年頃））」をもとに本プロジェクトで取得・結合。

詳細は `docs/SOURCE_AND_METHOD.md` を参照。
