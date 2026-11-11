# Resonite on Steam Frame

Steam FrameでSteam版Resoniteを動かすための非公式セットアップです。エンジンをネイティブARM64の.NETで、ブートストラッパーとUnityレンダラーをx64版Proton ExperimentalとFEXで実行します。起動、スピーカー・マイク、FBXモデルのインポート、動画URLの読み込み、画像変換に必要な対応をまとめています。

2026年11月11日から12日にかけて、SteamOS 0.5.8、Resonite `2026.11.11.21`、Steam build `26579448` で動作を確認しました。対応の経緯は[調査と修正の記録](docs/investigation.ja.md)、自動化の検証範囲は[テスト記録](docs/testing.ja.md)にまとめています。

## 事前に用意するもの

Steam Frame側にSteam版ResoniteとProton Experimentalをインストールしてください。Frame上の `python3`、`c++`、`cmake`、`make`、`ldd` も使います。システムパッケージのインストールや `sudo` は行いません。

導入前にResoniteを手動で終了してください。Steamが起動中でも、既存のローカル設定APIを使える場合は設定できます。APIを利用できない場合は変更前に停止するので、Steamも手動で終了してから再実行してください。インストーラーはゲームやSteamの終了・起動・再起動を行いません。

## クイックスタート

Frameのターミナルで実行してください。

```bash
curl -fsSL https://raw.githubusercontent.com/kazu0617/resonite-steam-frame/main/install.sh -o install-resonite-frame.sh && bash install-resonite-frame.sh
```

画像変換ライブラリをFrame上でビルドするため、導入には数分かかります。完了後は、SteamからResoniteを起動してください。既存の通常の起動引数を保持し、起動ヘルパーと `-Device SteamVR` を設定します。独自の `%command%` ラッパーがある場合は、勝手に組み替えずに停止します。

## チェックと復元

取得済みのスクリプトをFrame上で次のように実行します。`--check` は書き込みを伴わず、Resoniteの起動中でも使えます。

```bash
bash install-resonite-frame.sh --check
```

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
| 動画URL | ARM64版yt-dlp `2026.10.26` とDeno `2.9.8` を導入 |
| 画像変換 | CompressonatorのARM64向け修正を適用し、ビルドして差し替え |
| Steam起動オプション | `frame-resonite-launch %command% -Device SteamVR` を設定 |

ネイティブライブラリの検索パスとOpenSSL設定は、ARM64エンジンの起動にだけ適用します。

主な保存先は次のとおりです。Steamライブラリの場所は自動検出します。

```text
Resonite/dotnet-runtime-arm64/
Resonite/frame-libs-arm64/
Resonite/dotnet-runtime-arm64/tools/
Resonite/RuntimeData/yt-dlp_linux
Resonite/runtimes/linux-arm64/native/libmikktspace.so
Resonite/runtimes/linux-arm64/native/libCMP_*.so
~/.local/bin/frame-resonite-launch
~/.local/share/resonite-steam-frame/manifest.json
~/.local/share/resonite-steam-frame/backups/
```

`XDG_DATA_HOME` を設定している場合、導入記録とバックアップはその配下に保存します。`LinuxBootstrap.sh` は期待する形式と一致する箇所だけ変更し、元の内容を保存します。失敗した場合の復元にも `--uninstall` を使います。

## 対応範囲と制限

既定では検証済みbuildだけに適用します。Resoniteが更新された場合、起動ヘルパーは停止して再確認を求めます。未検証buildを確認しながら試すための `--allow-untested-build` もありますが、異なる.NET世代や未知の起動スクリプトには適用しません。

動画ツールはこのインストーラーでバージョンを管理します。更新時もクイックスタートのコマンドを実行してください。`v0.1.0` からの更新では、最初の導入前に保存したバックアップを引き継ぎます。

## 開発とテスト

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_installer.py
python3 tools/build_installer.py --check
bash -n install.sh
```

`install.sh` は `src/resonite_frame.py` から生成する自己完結スクリプトです。変更後は生成し直してください。Frame上で隔離した導入・復元を試す方法は[テスト記録](docs/testing.ja.md)にあります。

## ライセンス・外部ソース

- 本ツールのコードは[MIT License](LICENSE)です。
- Resonite、Microsoft .NET、各ネイティブライブラリには、それぞれのライセンスが適用されます。
- [mikktspace](https://github.com/Yellow-Dog-Man/Mikktspace.NET)と[Compressonator](https://github.com/Yellow-Dog-Man/compressonator)は、ソースのコミットとアーカイブのSHA256を固定しています。
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)と[Deno](https://github.com/denoland/deno)は、固定版のSHA256を検証します。
- .NETは、Microsoftの配布メタデータにあるSHA512で確認します。
