# A separate Dockerfile-multiple used to exist for MultiPL-E; the single
# Dockerfile now bundles every MultiPL-E language runtime directly, so
# there's only one image to build.
DOCKERFILE=Dockerfile
IMAGE_NAME=evaluation-harness

build:
	docker build -f $(DOCKERFILE) -t $(IMAGE_NAME) .

test:
	docker run -v $(CURDIR)/tests/docker_test/test_generations.json:/app/test_generations.json:ro \
	-it $(IMAGE_NAME) python3 main.py --model dummy_model --tasks humaneval --limit 4 \
	--load_generations_path /app/test_generations.json --allow_code_execution 

	@echo "If pass@1 is 0.25 then your configuration for standard benchmarks is correct"

all: build test