# Resonite on Steam Frame

Steam FrameでSteam版Resoniteを動かすための非公式セットアップです。エンジンをネイティブARM64の.NETで、ブートストラッパーとUnityレンダラーをx64版Proton ExperimentalとFEXで実行します。起動に加え、内蔵スピーカーへの音声出力とFBXモデルのインポートに必要な対応を含みます。

2026年11月11日に、SteamOS 0.5.8、Resonite `2026.11.11.21`、Steam build `26579448` で動作を確認しました。対応の経緯は[調査と修正の記録](docs/investigation.ja.md)、自動化の検証範囲は[テスト記録](docs/testing.ja.md)にまとめています。

## 事前に用意するもの

Steam Frame側にSteam版ResoniteとProton Experimentalをインストールしてください。Frame上の `python3`、`c++`、`ldd` も使います。システムパッケージのインストールや `sudo` は行いません。

導入前にResoniteを手動で終了してください。Steamが起動中でも、既存のローカル設定APIを使える場合は設定できます。APIを利用できない場合は変更前に停止するので、Steamも手動で終了してから再実行してください。インストーラーはゲームやSteamの終了・起動・再起動を行いません。

PrivateリポジトリからPC経由で導入する場合は、PC側の `gh` が認証済みで、`ssh frame` がSteam Frameへ接続できる必要があります。GitHubの認証情報をFrameへ渡すことはありません。

## ワンコマンドで導入する

### Privateの間はPCから実行

次の1行で、認証付きでスクリプトを取得してFrame上で実行します。取得に成功してから実行するため、ダウンロードが失敗した場合は適用しません。

```bash
gh api -H 'Accept: application/vnd.github.raw+json' 'repos/kazu0617/resonite-steam-frame/contents/install.sh?ref=v0.1.0' > install-resonite-frame.sh && ssh frame 'bash -s' < install-resonite-frame.sh
```

### Publicへ切り替えた後はFrameで実行

以下のURLは、リポジトリがPublicになってから認証なしで使えます。Frameのターミナルで実行してください。

```bash
curl -fsSL https://raw.githubusercontent.com/kazu0617/resonite-steam-frame/v0.1.0/install.sh -o install-resonite-frame.sh && bash install-resonite-frame.sh
```

完了後は、SteamからResoniteを起動してください。既存の通常の起動引数を保持し、起動ヘルパーと `-Device SteamVR` を設定します。独自の `%command%` ラッパーがある場合は、勝手に組み替えずに停止します。

## チェックと復元

取得済みのスクリプトをFrame上で次のように実行します。`--check` は書き込みを伴わず、Resoniteの起動中でも使えます。終了コード0は必要なファイルと対応する構造が見つかったことを示し、VRでの表示やコントローラー操作を保証するものではありません。

```bash
bash install-resonite-frame.sh --check
```

PCにスクリプトがある場合は `ssh frame 'bash -s -- --check' < install-resonite-frame.sh` でも確認できます。

導入内容を戻す場合はResoniteを手動で終了し、次のコマンドを実行してください。Steamを終了した状態で導入した場合は、復元時もSteamを終了してください。起動中SteamのAPIで導入した場合は、Steamを起動してAPIを使える状態で復元します。

```bash
bash install-resonite-frame.sh --uninstall
```

復元の対象は、変更したファイルとResonite用の起動オプション・Proton設定です。ほかのアプリのSteam設定は保持し、導入後に対象ファイルや対象設定が変更されていれば上書きせず停止します。バックアップと取得済みキャッシュは復元後も残ります。

## 変更する内容

| 対象 | 対応 |
| --- | --- |
| Proton | Resoniteだけを `proton_experimental` に設定 |
| 起動環境 | `PROTON_SET_GAME_DRIVE=0`、`FEX_TSOENABLED=1` を設定 |
| エンジン | ARM64版.NET `10.0.15` をゲーム内へ導入 |
| ICU・OpenSSL・音声 | FrameにあるARM64ライブラリと依存先をゲーム内へコピーし、OpenSSLのlegacy providerを有効化 |
| FBXインポート | 開発元のmikktspaceソースをARM64向けにビルドして追加 |
| Steam起動オプション | `frame-resonite-launch %command% -Device SteamVR` を設定 |

ネイティブライブラリの検索パスとOpenSSL設定は、ARM64エンジンの起動にだけ適用します。OSの音量、音声サービス、ほかのSteamアプリは変更しません。ゲーム本体や第三者のバイナリをこのリポジトリから再配布する構成にはしていません。

主な保存先は次のとおりです。Steamライブラリの場所は自動検出します。

```text
Resonite/dotnet-runtime-arm64/
Resonite/frame-libs-arm64/
Resonite/runtimes/linux-arm64/native/libmikktspace.so
~/.local/bin/frame-resonite-launch
~/.local/share/resonite-steam-frame/manifest.json
~/.local/share/resonite-steam-frame/backups/
```

`XDG_DATA_HOME` を設定している場合、導入記録とバックアップはその配下に保存します。`LinuxBootstrap.sh` は期待する形式と一致する箇所だけ変更し、元の内容を保存します。失敗した場合の復元にも `--uninstall` を使います。

## 対応範囲と制限

既定では検証済みbuildだけに適用します。Resoniteが更新された場合、起動ヘルパーは停止して再確認を求めます。未検証buildを確認しながら試すための `--allow-untested-build` もありますが、異なる.NET世代や未知の起動スクリプトには適用しません。

SignalRの同期障害はこのインストーラーでは修正しません。調査中はPC側でも同じ接続エラーが出ており、その後にコンタクト初期化の成功を確認しています。同期初期化待ちがダッシュの位置変更に影響するコード経路は確認できましたが、メニュー操作全般の不具合を解決したわけではありません。

動画URLの取り込みではx64版 `yt-dlp_linux` の実行形式エラー、画像変換の一部では `CMP_Compressonator` の読み込み失敗も観測しました。この版の修正対象には含めていません。マイクはデバイス認識まで確認しており、実際の会話は未検証です。

## 開発とテスト

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_installer.py
python3 tools/build_installer.py --check
bash -n install.sh
```

`install.sh` は `src/resonite_frame.py` から生成する自己完結スクリプトです。変更後は生成し直してください。Frame上で隔離した導入・復元を試す方法は[テスト記録](docs/testing.ja.md)にあります。

本ツールのコードはMIT Licenseです。Resonite、Microsoft .NET、各ネイティブライブラリにはそれぞれのライセンスが適用されます。mikktspaceは[開発元のリポジトリ](https://github.com/Yellow-Dog-Man/Mikktspace.NET)のコミットとアーカイブのSHA256を固定し、.NETはMicrosoftの配布メタデータにあるSHA512で確認します。
