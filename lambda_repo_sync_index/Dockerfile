FROM public.ecr.aws/lambda/python:3.12

RUN dnf install -y git make gcc gcc-c++ findutils tar gzip \
    && dnf clean all

COPY . /var/task

RUN python -m pip install --upgrade pip \
    && python -m pip install /var/task \
    && make -C /var/task/vendor/codebase_memory_pipeline repo-graph-export

CMD ["lambda_repo_sync_index.lambda_function.handler"]
