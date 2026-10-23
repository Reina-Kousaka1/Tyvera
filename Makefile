.PHONY: test hello check

test:
	python -m unittest discover -v

hello:
	python -m tyvera run hello.tyv

check:
	python -m tyvera check hello.tyv
	python -m tyvera check examples/score.tyv
