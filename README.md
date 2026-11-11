# Resonite on Steam Frame

Steam FrameでSteam版Resoniteを動かすための非公式セットアップです。起動、音声入出力、FBXモデルのインポート、動画URLの読み込み、画像変換に必要な設定と実行環境をまとめて導入します。

SteamOS 0.5.8、Resonite `2026.11.11.21`（Steam build `26579448`）で動作を確認しています。

## クイックスタート

Steam FrameにSteam版ResoniteとProton Experimentalをインストールしてください。Resoniteを終了し、Frameのターミナルで実行します。

```bash
curl -fsSL https://raw.githubusercontent.com/kazu0617/resonite-steam-frame/main/install.sh -o install-resonite-frame.sh && bash install-resonite-frame.sh
```

導入には数分かかります。完了後はSteamからResoniteを起動してください。更新時も同じコマンドを使います。

## チェックと復元

導入状態を確認します。Resoniteの起動中でも実行できます。

```bash
bash install-resonite-frame.sh --check
```

導入前に戻すには、Resoniteを終了して次のコマンドを実行してください。Steamの起動・終了状態は、導入したときとそろえてください。

```bash
bash install-resonite-frame.sh --uninstall
```

## 詳しい記録

- [調査と修正の記録](docs/investigation.ja.md)
- [テスト・開発手順](docs/testing.ja.md)

## ライセンス・外部ソース

- 本ツールのコードは[MIT License](LICENSE)です。
- Resonite、Microsoft .NET、各ネイティブライブラリには、それぞれのライセンスが適用されます。
- [Microsoft .NET](https://dotnet.microsoft.com/)：Resoniteエンジンの実行環境
- [mikktspace](https://github.com/Yellow-Dog-Man/Mikktspace.NET)：FBXモデルのインポートに使用
- [Compressonator](https://github.com/Yellow-Dog-Man/compressonator)：画像の圧縮・変換に使用
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)と[Deno](https://github.com/denoland/deno)：動画URLの読み込みに使用
