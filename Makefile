.PHONY: up down logs health test-render bootstrap

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f worker n8n

health:
	docker compose exec worker python scripts/health_check.py

bootstrap:
	python scripts/generate_dummy_assets.py

test-render:
	python scripts/generate_video.py \
		--images test_assets/images/puzzle_1.png test_assets/images/puzzle_2.png \
		         test_assets/images/puzzle_3.png test_assets/images/puzzle_4.png \
		--titles "Can you solve this?" "Only 1% get it right" "What comes next?" "Final challenge" \
		--captions "Drop your guess below 👇" "Comment your answer!" "What do YOU think?" "Were you right? 🔥" \
		--output-dir ./output

flower:
	open http://localhost:5555
