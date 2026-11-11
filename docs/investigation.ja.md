# Steam FrameでResoniteを起動し、音声とモデルインポートを使えるようにした記録

2026年11月11日、Steam Frame上のResoniteを調査し、VRでの起動、内蔵スピーカーの音声出力、FBXモデルの表示まで確認しました。Steam FrameにはSSHで接続し、起動ログと実行環境を調べながら対応しました。

検証時の環境はSteamOS 0.5.8、build `20261106.6144341`、ARM64です。ResoniteはSteam app `2519830`、build `26579448`、バージョン `2026.11.11.21` を使いました。

## インストールと最初の起動

当初はResoniteを指すショートカットがありましたが、Steam側にはゲーム本体が入っていませんでした。Steam版をインストールし、必要な実行環境も取得しました。

既定のARM64版Proton 11.0-2で起動すると、ブートストラッパーのログが出る前に `BadImageFormatException`、エラーコード `0x8007000B` で停止しました。スタックには `Cloudtoid.Interprocess.Semaphore.Wine.Interop.SemaphoreOpen` があり、同梱の `shmbridge.dll.so` はx86-64のELFでした。ARM64版Wineからこの経路を使う構成では起動できなかったため、x64版Proton ExperimentalとFEXへ切り替えました。

この変更で次の段階へ進みましたが、今度はレンダラーを起動する際にWin32エラー267が出ました。ProtonのS:ゲームドライブによって、Linuxのパスが期待しない作業ディレクトリとして解釈されていました。`PROTON_SET_GAME_DRIVE=0` を設定し、Z:経由のパスに戻すことでレンダラーを起動できました。

## ARM64エンジンとx64レンダラーに分ける

エンジンもx64のままFEXで動かすと、初期化中にブートストラッパーとのheartbeatがタイムアウトしました。タイムアウトの低レベルの原因までは特定できていません。

そこで、エンジンの `Renderite.Host.dll` だけをネイティブARM64の.NET `10.0.15` で実行し、ブートストラッパーとUnityレンダラーはx64版Proton ExperimentalとFEXで実行する構成にしました。`FEX_TSOENABLED=1` も設定しています。この値は動作確認時に使った条件であり、単独での効果は比較検証していません。

```text
Steam
  └─ 起動ヘルパー
      └─ x64版Proton Experimental + FEX
          ├─ ブートストラッパー
          ├─ Unityレンダラー → SteamVR → Steam Frame
          └─ LinuxBootstrap.sh
              └─ ネイティブARM64 .NET → Renderite.Host.dll
```

ARM64エンジンを動かすと、Steamの混在実行環境には不足しているARM64ライブラリがあることも分かりました。最初にICU、次にOpenSSLが必要になり、さらにユーザースペースを作る処理でDESが使えず停止しました。

FrameのOSにあるICU 78.2、`libssl.so.3`、`libcrypto.so.3` とOpenSSLのlegacy providerを、ゲーム内の専用ディレクトリへコピーしました。OpenSSL設定とライブラリ検索パスはARM64エンジンの実行時だけに渡しています。OSのパッケージやグローバルな設定は変更していません。

この構成では初期化が約10〜12秒で完了しました。22:14にはレンダラーの出力先がSteamVRとなり、Steam Frame HMDの認識とVR起動をログで確認できました。画面モードでも日本語の初期画面とチュートリアルを表示できています。

## 内蔵スピーカーから音が出ない問題

起動後も音が聞こえず、ログ上のデバイスは `NULL Playback Device` と `NULL Capture Device` だけでした。OS側のスピーカー音量と出力先は正常でしたが、エンジンの音声ストリームが作られていませんでした。

原因は、ネイティブARM64エンジンから使うPulseAudioクライアントライブラリの不足でした。Frameにある `libpulse.so.0`、`libpulsecommon-17.0.so` と必要な依存ライブラリを、同じゲーム内ディレクトリへ追加しました。この音声対応時にはResoniteを再起動しています。

再起動後は `Built-in Audio Playback` と `Built-in Audio Capture` を認識し、エンジンが内蔵スピーカーへ音声ストリームを出していることを確認できました。ストリームから信号も検出でき、装着していたユーザーからも音が聞こえるとの確認を得ました。OSの音量や音声サービスの設定は変更していません。マイクは認識までの確認で、会話のテストは行っていません。

## FBXモデルのインポートが途中で止まる問題

モデルをインポートした際、ログにはシーンを読み取った記録の後に、次のエラーが出ていました。

```text
Imported scene. Meshes: 1, Materials: 1, Animations: 0, Textures: 0
System.DllNotFoundException: Unable to load shared library 'mikktspace'
ModelImporter.ImportMesh
  → MeshX.RecalculateTangentsMikktspace
  → Native.genMikktTangSpace
```

FBXの読み取りは通っていましたが、メッシュの接線を計算するmikktspaceのARM64版がありませんでした。同梱パッケージ `YellowDogMan.Mikktspace.Net/0.1.4` にはLinux x64、Windows x64、Android ARM用のネイティブライブラリがあり、Linux ARM64用は含まれていませんでした。

[開発元のMikktspace.NET](https://github.com/Yellow-Dog-Man/Mikktspace.NET)からソースを取得し、Frame上でARM64の共有ライブラリをビルドしました。使用したコミットは `a25b708d852d88848d4c729677cd8dbad4526523` です。このコミットのネイティブソースがタグ `0.1.4` の内容と同じこと、ゲーム同梱の.NETラッパーが要求する `genMikktTangSpace` を公開していることを確認しました。

ゲームとは別のARM64 .NETプロセスで、ゲーム同梱の `Mikktspace.NET.dll` と `AssimpNet.dll` を使って検証しました。既知の三角形では期待する接線と一致し、実際に失敗していた2つのFBXでも接線の計算が完了しました。片方は164面、もう片方は1,312面で、各頂点の計算結果が有限値になり、必要なコールバックがすべて呼ばれています。

完成した `libmikktspace.so` を `runtimes/linux-arm64/native/` に追加すると、起動中のエンジンがそのまま読み込みました。再インポート後にモデルが表示されたこともユーザーが確認しています。この対応では再起動していません。

## ログイン同期とダッシュ操作の調査

認証自体は成功していましたが、コンタクトやオンライン状態を初期化するSignalR接続が、WebSocketの `InternalServerError` で繰り返し切断されていました。PC側の同じバージョンのログにも同様のエラーがあったため、Frame固有の問題とは断定していません。22:38にはコンタクト初期化の成功が記録され、その後は再接続エラーが止まりました。復旧理由は特定できていません。

同期エラー中にダッシュを動かせないとの報告もあり、ダッシュの位置・大きさを保存する処理を確認しました。新しいクラウド変数の初期化はコンタクト一覧の読み込み完了を待ちます。その待機中は値を書き込めないため、掴む判定が通っても位置の変更が反映されないコード経路がありました。Astra Mediumにもこの因果関係のレビューを依頼しました。

ただし、実行中の対象変数の状態を直接取得したわけではありません。初期化済みの変数なら、再接続中も有効な値を保持する場合があります。したがって、同期エラーが出ると常にダッシュを動かせなくなる、とまでは言えません。ほかのメニュー操作への影響も未確定です。

## 自動化に含めた範囲

`install.sh` には、Proton設定、起動環境、ARM64 .NET、ICU・OpenSSL・PulseAudio、mikktspaceの対応をまとめました。必要なファイルを一時領域で準備・検証してから適用し、Steam設定は最後に更新します。バックアップと変更記録を残し、再実行と復元に対応しています。

手動対応で動いた構成と、インストーラーの自動適用で確認した範囲は分けて記録しています。実際のFrameでは読み取り専用チェックを行い、自動導入・再実行・復元はFrame上に作った隔離環境で検証しています。すでに動いているResoniteを止めて再導入したわけではありません。

動画URLの取り込みで観測した `yt-dlp_linux` の実行形式エラー、画像変換の一部で観測した `CMP_Compressonator` の読み込み失敗、SignalRの接続障害は今回の自動修正には含めていません。

## 参照先

- [Resonite Wiki: Linux notes](https://wiki.resonite.com/Linux_notes)
- [Resonite Wiki: Command Line Arguments](https://wiki.resonite.com/Command_Line_Arguments)
- [Wine-ShmBridge](https://github.com/Yellow-Dog-Man/Wine-ShmBridge)
- [Proton issue #8824](https://github.com/ValveSoftware/Proton/issues/8824)
- [OpenSSL legacy provider](https://docs.openssl.org/3.6/man7/OSSL_PROVIDER-legacy/)
- [YellowDogMan.Mikktspace.Net 0.1.4](https://www.nuget.org/packages/YellowDogMan.Mikktspace.Net/0.1.4)
