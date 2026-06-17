.PHONY: run dev install build autostart autostart-remove

install:
	cd backend && uv sync
	cd frontend && npm install

build:
	cd frontend && npm run build

run: build
	cd backend && uv run uvicorn app.main:app --port 7777

# frontend hot-reload on :5173 proxying to the backend on :7777
dev:
	cd frontend && npm run dev

autostart:
	sed "s|__REPO__|$(CURDIR)|g; s|__UV__|$$(command -v uv)|g" ops/io.arccos.cos.plist \
		> ~/Library/LaunchAgents/io.arccos.cos.plist
	launchctl unload ~/Library/LaunchAgents/io.arccos.cos.plist 2>/dev/null || true
	launchctl load ~/Library/LaunchAgents/io.arccos.cos.plist
	@echo "arc-CoS now starts on login. Board: http://localhost:7777"

autostart-remove:
	launchctl unload ~/Library/LaunchAgents/io.arccos.cos.plist 2>/dev/null || true
	rm -f ~/Library/LaunchAgents/io.arccos.cos.plist
