# Transcript Viewer

A simple, standalone web application to view execution transcripts from the `debug/` directory.

## How to use

1.  Open `index.html` in any modern web browser.
2.  Click **"Select Debug Folder"**.
3.  Choose the `debug/` folder in your project root.
4.  The app will automatically find and list all `transcript.json` files.
5.  Select a transcript from the sidebar to view the conversation and tool calls.

## Features

- **No Server Required**: Works directly via `file://` protocol.
- **Folder Support**: Select the entire `debug/` directory to load all transcripts at once.
- **Role Highlighting**: Distinct styles for System, User, and Assistant messages.
- **Tool Call Detection**: Visualizes tool calls and their arguments.
- **Markdown Support**: Basic rendering of code blocks.
