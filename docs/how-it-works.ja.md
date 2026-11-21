# 仕組み（内部動作）

このドキュメントは、`install.sh` / `src/resonite_frame.py` が Steam Frame 上で何をしているかを、コードに基づいて整理したものです。利用方法は [README](../README.md) を参照してください。

- 対象バージョン: `VERSION = "0.3.2"`（`src/resonite_frame.py`）
- 対象アプリ: Steam App ID `2519830`（Resonite）

## 概要

Resonite の Steam 版は Windows 向けの本体と、Linux 向けの起動スクリプト（`LinuxBootstrap.sh`）を同梱しています。Steam Frame は ARM64 の Linux 端末のため、そのままでは次の 2 点が噛み合いません。

1. 起動スクリプトが x86-64 前提の .NET を要求する
2. モデル変換・画像変換・動画読み込みなどで使うネイティブライブラリが Windows / x86-64 前提

このインストーラーは、Steam の起動経路を差し替えたうえで、Resonite のゲームフォルダー内に ARM64 版の .NET ランタイムとネイティブライブラリを配置し、起動スクリプトと Steam 設定を書き換えることで、Proton Experimental 上の Linux 起動経路を ARM64 で通します。変更はすべて導入記録（manifest）に残し、`--uninstall` で元に戻せるようにしています。

## 処理の流れ

`--install` を実行したときの大まかな順序は次のとおりです。

1. 前提条件の検証（Resonite が起動中でない、Steam の更新中でない、必要なコマンドと Proton Experimental がある）
2. `LinuxBootstrap.sh` の書き換え可否を先に検証
3. Steam の現在の起動オプションと互換ツール設定を読み取る
4. 一時ディレクトリで以下を準備
   - ホスト側 ARM64 ライブラリの収集
   - ARM64 .NET ランタイムの取得
   - Mikktspace のビルド
   - yt-dlp / Deno の取得
   - Compressonator のビルド
5. 準備中にゲームや Steam 設定が変わっていないことを再確認
6. トランザクションとしてゲームフォルダー、起動ヘルパー、Steam 設定を適用
7. 導入記録を `installed` で確定

準備はすべて一時ディレクトリで行い、ゲームフォルダーへの書き込みは手順 6 にまとめています。

## 1. Steam の起動経路の差し替え

`launch_options()` は Steam の Resonite の起動オプションを次の形に書き換えます。

```
<ヘルパーの絶対パス> %command% -Device SteamVR
```

ヘルパーは既定で `~/.local/bin/frame-resonite-launch` に生成されます（`--bin-dir` で変更可）。中身は `~/.local/share/resonite-steam-frame/lib` を `sys.path` に追加し、`resonite_frame.launch()` を呼ぶだけの小さな Python スクリプトです。

`launch()` は次を確認してから、Steam が渡したコマンドを `os.execvpe` で実行します。

- 導入記録が `installed` であること
- 導入時に記録した必須ファイルがすべて存在すること
- `Renderite.Host.frame.runtimeconfig.json` とゲーム側の `Renderite.Host.runtimeconfig.json` から生成した内容が一致すること
- 要求を満たす ARM64 .NET がゲームフォルダー内にあること
- `LinuxBootstrap.sh` が記録どおりに書き換え済みであること

実行時は環境変数 `PROTON_SET_GAME_DRIVE=0` と `FEX_TSOENABLED=1` を追加します。FEX 側の x86 TSO エミュレーションを有効にするための設定です。

既存の起動オプションに独自の `%command%` 記法がある場合や、解釈できない内容が含まれる場合は、推測で書き換えずにエラーにします。

## 2. `LinuxBootstrap.sh` の書き換え

`patch_bootstrap()` は既知の構成のスクリプトだけを対象に、次の 4 点を書き換えます。

| 項目 | 変更内容 |
| --- | --- |
| .NET の配置先 | `DOTNET_ROOT` を `$PWD/dotnet-runtime` から `$PWD/dotnet-runtime-arm64` へ |
| .NET の導入引数 | `dotnet-install` の呼び出しを `--version <必要バージョン> --runtime dotnet --architecture arm64` に固定 |
| OpenSSL とライブラリ探索 | `OPENSSL_MODULES` / `OPENSSL_CONF` / `LD_LIBRARY_PATH` を `frame-libs-arm64` に向ける |
| エンジンの起動 | `dotnet exec --runtimeconfig "$PWD/Renderite.Host.frame.runtimeconfig.json" Renderite.Host.dll "$@"` |

書き換えは冪等で、対象の文字列が想定どおり 1 箇所だけ存在することを確認してから行います。構成が変わっていた場合は `bootstrap_unsupported` として停止します。

## 3. ARM64 .NET ランタイム

`runtime_requirement()` がゲーム側の `Renderite.Host.runtimeconfig.json` を読み、要求される .NET のバージョンとロールフォワード方針を決めます。プレビュー版や `rollForward: disable` の場合は完全一致、それ以外は同じメジャー・マイナー系の最新パッチを選びます。

`stage_runtime()` は次の順でランタイムを用意します。

1. ゲームフォルダー内の `dotnet-runtime-arm64` に要件を満たす ARM64 ランタイムがあれば再利用（ネットワーク不要）
2. なければ `https://builds.dotnet.microsoft.com/dotnet/release-metadata/<major.minor>/releases.json` を取得
3. `linux-arm64` の `dotnet-runtime-linux-arm64.tar.gz` を選択
4. 配布元が HTTPS かつ Microsoft 系ホストであること、SHA-512 の記載があることを確認
5. ダウンロードして SHA-512 を検証し、展開
6. `dotnet` と `libcoreclr.so` が ARM64 の ELF であることを確認

展開先はゲームフォルダーの `dotnet-runtime-arm64/` です。

## 4. Windows 用 runtimeconfig の正規化

ゲーム側の `Renderite.Host.runtimeconfig.json` は Windows デスクトップ向けに `Microsoft.WindowsDesktop.App` を含むことがあります。`linux_runtime_config()` はこれを読み、`Microsoft.NETCore.App` だけを含む設定を `Renderite.Host.frame.runtimeconfig.json` として生成します。

- 元の `Renderite.Host.runtimeconfig.json` は変更しません（Steam の Windows 構成を保つため）
- 想定外のフレームワーク名や重複がある場合はエラーにします
- 生成した設定は必須ファイルとして導入記録に含め、起動時とチェック時に照合します

## 5. ホスト側 ARM64 ライブラリの収集

`stage_host_libraries()` は SteamOS 側の `/usr/lib` と `/usr/lib/aarch64-linux-gnu` から、次のライブラリとその依存を収集して `frame-libs-arm64/` にコピーします。

- ICU: `libicuuc.so`, `libicui18n.so`, `libicudata.so`
- OpenSSL 3: `libssl.so.3`, `libcrypto.so.3`
- PulseAudio: `libpulse.so.0`, `pulseaudio/libpulsecommon-*.so`
- OpenSSL の legacy provider: `ossl-modules/legacy.so`

依存は `ldd` の出力をたどって再帰的に集めます。ただし `libc.so` や `ld-linux` などのコアライブラリ（`CORE_LIBS`）と、ローダー自身の絶対パスは対象外にします。収集した各ファイルは ARM64 の ELF かどうかを確認し、`ldd` で `not found` が出るものはエラーにします。

あわせて `openssl.cnf` を生成し、legacy provider を有効化します。ゲーム側の起動スクリプトはこの設定を `OPENSSL_CONF` で参照します。

## 6. Mikktspace（FBX モデルのインポート）

Resonite は FBX のインポートに Mikktspace を使います。`stage_mikk()` は Yellow Dog Man の `Mikktspace.NET` を固定コミット（`a25b708d…`）から取得し、SHA-256 を検証したうえで、`mikktspace.c` と `mikktspace_api.cpp` を `c++ -O3 -fPIC -shared` でビルドして `runtimes/linux-arm64/native/libmikktspace.so` に配置します。

ビルド後は `probe_mikk()` が ctypes で実際に `genMikktTangSpace()` を呼び、既知の三角形に対してコールバックと接線計算が期待どおり動くかを確認します。すでに ARM64 の `libmikktspace.so` がある場合は、その動作確認だけを行って再利用します。

## 7. Compressonator（画像変換）

画像の圧縮・変換には Yellow Dog Man の Compressonator を使います。`stage_compressonator()` は固定コミット（`090a9c12…`）のソースを取得し、SHA-256 を検証してから、ARM64 向けに次のパッチを当てて CMake でビルドします。

- `cmp_core/shaders/bc1_cmp.h`: ARM64 では SIMD 関数ポインターの初期化を静的実装に固定
- `applications/_plugins/common/pluginbase.h`: `<cstdint>` を追加

ビルド対象は `CMP_Compressonator` のみで、GUI や Qt、KTX2、EXR などは無効化しています。成果物 `libCMP_Framework.so` と `libCMP_Compressonator.so` を `runtimes/linux-arm64/native/` に配置します。

ビルド後は小さな C++ プログラム（`CMP_PROBE`）をコンパイルして実行し、BC1 / BC3 / BC7 の圧縮と展開が通ること、復元誤差が許容範囲に収まることを確認します。失敗時はビルドログを `~/.local/share/resonite-steam-frame/cache/compressonator-build.log` に保存します。

## 8. 動画 URL の読み込み（yt-dlp + Deno）

`stage_video()` は次の 2 つを取得します。

- `yt-dlp` `2026.10.26`（`yt-dlp_linux_aarch64`）
- Deno `2.9.8`（`deno-aarch64-unknown-linux-gnu.zip` 内の `deno`）

どちらも SHA-256 を検証し、ARM64 の ELF であることと `--version` が実行できることを確認したうえで、`dotnet-runtime-arm64/tools/` に配置します。

Resonite が呼ぶ `RuntimeData/yt-dlp_linux` は、インストーラーが管理するシェルスクリプトに置き換えます（`.backup` にも同じ内容を置き、Resonite 側のロールバックで上書きされても戻るようにしています）。このランチャーは次を行います。

- 通常の引数はそのまま `tools/yt-dlp_linux_aarch64` に渡す
- `-U` / `--update` 系の引数では自己更新せず、インストーラーでの更新を案内する（`--update-to=nightly` はメッセージのみで正常終了）
- `tools/` を `PATH` に追加してから実行する

これにより、yt-dlp の自己更新が ARM64 管理下のバイナリを壊さないようにしています。

## 9. Steam 設定の書き換え

`Settings` クラスが Steam の 2 つの設定を扱います。

| 設定 | 内容 |
| --- | --- |
| 起動オプション | `launch_options()` が生成した `<helper> %command% -Device SteamVR` |
| 互換ツール | `CompatToolMapping[2519830]` を `name = proton_experimental`, `config = ""`, `priority = "250"` に |

読み書きの方法は Steam の起動状態で変わります。

### Steam 起動中（CDP 経由）

Steam クライアントのループバックデバッグポート（`127.0.0.1:8080`）に WebSocket で接続し、`SharedJSContext` に対して JavaScript を評価します。

- 読み取り: `appDetailsStore.RequestAppDetails` → `GetAppDetails`
- 書き込み: `SteamClient.Apps.SetAppLaunchOptions` → `SteamClient.Apps.SpecifyCompatTool`
- アカウント確認: `loginStore.accountName`

`DebugSocket` はこの用途に必要な最小限の WebSocket 実装で、接続先を `ws://127.0.0.1:8080` に限定しています。書き込み前後にはアカウントが切り替わっていないことを確認します。

### Steam 終了中（VDF 直接編集）

次のファイルを直接読み書きします。

- `config/config.vdf` の `InstallConfigStore > Software > Valve > Steam > CompatToolMapping > 2519830`
- `userdata/<アカウントID>/config/localconfig.vdf` の `UserLocalConfigStore > Software > Valve > Steam > apps > 2519830`

アカウントは `LastOwner`（SteamID64 の下位 32 bit）、`--user-id`、`loginusers.vdf` から特定します。複数候補があって絞れない場合はエラーにします。

## 10. 導入のトランザクションと復元

`Transaction` クラスが導入記録（`~/.local/share/resonite-steam-frame/manifest.json`）とバックアップを管理します。

- 状態: `applying` → `installed`（失敗時は `failed`、復元後は `uninstalled`）
- 各ファイルの `before` / `after`（種別・SHA-256・パーミッション）を記録
- 変更前のファイルは `backups/<タイムスタンプ>/` に保存
- 書き込み前に記録を確定させ、書き込み直前の再確認で競合を検出
- ユーザーが導入後にファイルを編集していた場合は上書きせず停止

VDF は対象キーだけを記録するため、復元時もそのキーだけを戻し、他のアプリや他の設定の変更は保持します。

ゲームの更新で `LinuxBootstrap.sh` が Steam に差し戻された場合、既知の未パッチ構成であれば、その新しいスクリプトを今回バージョンの復元点として扱い、更新を継続できます。

## 生成・配置されるファイル

| パス | 内容 |
| --- | --- |
| `<game>/dotnet-runtime-arm64/` | ARM64 の .NET ランタイム一式 |
| `<game>/dotnet-runtime-arm64/tools/yt-dlp_linux_aarch64` | yt-dlp（ARM64） |
| `<game>/dotnet-runtime-arm64/tools/deno` | Deno（ARM64） |
| `<game>/frame-libs-arm64/` | ICU、OpenSSL 3、PulseAudio、legacy provider、`openssl.cnf` と依存 |
| `<game>/runtimes/linux-arm64/native/libmikktspace.so` | Mikktspace |
| `<game>/runtimes/linux-arm64/native/libCMP_Framework.so` | Compressonator |
| `<game>/runtimes/linux-arm64/native/libCMP_Compressonator.so` | Compressonator |
| `<game>/Renderite.Host.frame.runtimeconfig.json` | Linux 用に正規化した .NET 設定 |
| `<game>/LinuxBootstrap.sh` | 書き換え済みの起動スクリプト |
| `<game>/RuntimeData/yt-dlp_linux`, `yt-dlp_linux.backup` | 管理用 yt-dlp ランチャー |
| `~/.local/bin/frame-resonite-launch` | 起動ヘルパー（既定） |
| `~/.local/share/resonite-steam-frame/manifest.json` | 導入記録 |
| `~/.local/share/resonite-steam-frame/backups/` | 変更前ファイルのバックアップ |
| `~/.local/share/resonite-steam-frame/cache/` | ダウンロードとビルドログ |
| `~/.local/share/resonite-steam-frame/lib/` | ヘルパーが使う本体とロケール |

`--state-dir` と `--bin-dir` で保存先を変更できます。

## `--check` が見る項目

`check_report()` は JSON を返し、メニューから実行した場合は whiptail で表示します。主な確認項目は次のとおりです。

- `installation_complete`: 導入記録とゲーム・Steam の保存先が一致し、状態が `installed`
- `managed_files`: 導入後に管理対象ファイルが変わっていない
- `steam_settings_match`: Steam の起動オプションと互換ツールが導入時の内容と一致
- `runtime_arm64` / `runtime_version_available`: ARM64 の .NET と要求を満たすバージョンがある
- `runtime_configuration`: `Renderite.Host.frame.runtimeconfig.json` が現在のゲーム設定と一致
- `bootstrap_supported`: `LinuxBootstrap.sh` が想定どおり書き換え済み
- `support_libraries`: ICU、OpenSSL、PulseAudio、legacy provider、`openssl.cnf` がそろっている
- `mikktspace_arm64` / `compressonator_arm64`: 各ネイティブライブラリが ARM64
- `video_launcher` / `video_tools_arm64`: yt-dlp ランチャーと Deno / yt-dlp が ARM64
- `changed_files`: 導入後に変わったファイルの一覧

チェックは読み取りのみで、ファイルを書き換えません。

## アンインストール

`--uninstall` は次の順で元に戻します。

1. Resonite が起動していないことを確認
2. 導入時の Steam の起動状態（CDP / オフライン）と現在の状態が一致することを確認
3. 現在の起動オプションが「導入後」または「導入前」のどちらかであることを確認
4. Steam 設定を導入前に戻す
5. バックアップからファイルを復元
6. 状態を `uninstalled` に

導入後に手動でファイルを変更していた場合や、起動オプションが第三の値に変わっている場合は、上書きせずに停止します。

## インストーラ本体の生成

`install.sh` は手で編集せず、`src/resonite_frame.py` と `src/locales/*.json` から生成します。

```bash
python3 tools/build_installer.py
```

生成物は Python 本体とロケールをヒアドキュメントで埋め込んだ単一の bash スクリプトで、実行時に一時ディレクトリへ展開して `python3` で実行します。`--check` を付けると、`install.sh` が最新かどうかだけを検証します。

## テストと CI

`.github/workflows/check.yml` は push と pull request で次を実行します。

- `python3 -m unittest discover -s tests -v`
- `python3 tools/build_installer.py --check`（`install.sh` の再生成漏れ検出）
- `bash -n install.sh`（構文チェック）

`tests/test_installer.py` は、起動スクリプトの書き換え、VDF の読み書き、トランザクションと復元、アーカイブの安全性、ランタイム選択、ロケールの整合性、アカウント特定、メニュー遷移などを検証しています。

## 安全策の要点

- 実行環境は Linux の ARM64 のみ、`root` では実行不可
- ダウンロードは HTTPS のみ、SHA-256 / SHA-512 で検証
- ダウンロードは 256 MiB、展開は 1 GiB を上限とし、`..` や絶対パスを含むアーカイブは拒否
- 配置するバイナリは ARM64 の ELF であることを確認
- Mikktspace と Compressonator はビルド後に実際の関数を呼んで動作確認
- ゲーム起動中・Steam 更新中・設定変更中は書き込みを行わない
- 導入記録にない変更を検出した場合は、自動で上書き・復元しない

## 制約

- Steam 版 Resonite と Proton Experimental がインストール済みであることが前提
- ビルドのため `c++`、`ldd`、`cmake`、`make` が必要
- Compressonator のビルドには数分かかる
- Steam の内部 API（CDP の `SharedJSContext`）と VDF の構造に依存するため、Steam 側の仕様変更で動かなくなる可能性がある
- 起動スクリプトやゲーム側の構成が変わった場合は、対応版のインストーラーが必要
