"""
Runner class


You should define a json config file and place it into the /experiments folders
A CustomDataset class should be implemented, as well as a nn.Module, a Vectorizer and a Vocabulary (if the initial class is insufficient for the need)

This file aims at launching an experiments based on a config file

"""

import logging
from typing import Dict

import torch
from ignite.contrib.handlers.tensorboard_logger import TensorboardLogger, OutputHandler, OptimizerParamsHandler, WeightsScalarHandler, WeightsHistHandler, \
    GradsScalarHandler, GradsHistHandler
from ignite.engine import Events
from ignite.handlers import EarlyStopping
from ignite.handlers import ModelCheckpoint, TerminateOnNan
from knockknock import slack_sender

from transfer_nlp.runners.runnersABC import RunnerABC

name = 'transfer_nlp.runners.single_task'
logging.getLogger(name).setLevel(level=logging.INFO)
logger = logging.getLogger(name)


class Runner(RunnerABC):

    def __init__(self, config_args: Dict):

        super().__init__(config_args=config_args)

        # We show here how to add some events: tensorboard logs!
        tb_logger = TensorboardLogger(log_dir=self.config_args['logs'])
        tb_logger.attach(self.trainer,
                         log_handler=OutputHandler(tag="training", output_transform=lambda loss: {
                             'loss': loss}),
                         event_name=Events.ITERATION_COMPLETED)
        tb_logger.attach(self.evaluator,
                         log_handler=OutputHandler(tag="validation",
                                                   metric_names=["loss", "accuracy"],
                                                   another_engine=self.trainer),
                         event_name=Events.EPOCH_COMPLETED)
        tb_logger.attach(self.trainer,
                         log_handler=OptimizerParamsHandler(self.optimizer),
                         event_name=Events.ITERATION_STARTED)
        tb_logger.attach(self.trainer,
                         log_handler=WeightsScalarHandler(self.model),
                         event_name=Events.ITERATION_COMPLETED)
        tb_logger.attach(self.trainer,
                         log_handler=WeightsHistHandler(self.model),
                         event_name=Events.EPOCH_COMPLETED)
        tb_logger.attach(self.trainer,
                         log_handler=GradsScalarHandler(self.model),
                         event_name=Events.ITERATION_COMPLETED)
        tb_logger.attach(self.trainer,
                         log_handler=GradsHistHandler(self.model),
                         event_name=Events.EPOCH_COMPLETED)

        # This is important to close the tensorboard file logger
        @self.trainer.on(Events.COMPLETED)
        def end_tensorboard(trainer):
            logger.info("Training completed")
            tb_logger.close()

        @self.trainer.on(Events.EPOCH_COMPLETED)
        def log_embeddings(trainer):
            if hasattr(self.model, "embedding"):
                logger.info("Logging embeddings to Tensorboard!")
                embeddings = self.model.embedding.weight.data
                metadata = [str(self.vectorizer.data_vocab._id2token[token_index]).encode('utf-8') for token_index in range(embeddings.shape[0])]
                self.writer.add_embedding(mat=embeddings, metadata=metadata, global_step=self.trainer.state.epoch)

        handler = ModelCheckpoint(dirname=self.config_args['save_dir'], filename_prefix='experiment', save_interval=2, n_saved=2, create_dir=True, require_empty=False)
        self.trainer.add_event_handler(Events.EPOCH_COMPLETED, handler, {
            'mymodel': self.model})

        def score_function(engine):
            val_loss = engine.state.metrics['loss']
            return -val_loss

        handler = EarlyStopping(patience=10, score_function=score_function, trainer=self.trainer)
        # Note: the handler is attached to an *Evaluator* (runs one epoch on validation dataset).
        self.evaluator.add_event_handler(Events.COMPLETED, handler)

        # Terminate if NaNs are created after an iteration
        self.trainer.add_event_handler(Events.ITERATION_COMPLETED, TerminateOnNan())

    def update(self, batch_dict: Dict, running_loss: float, batch_index: int, running_metrics: Dict, compute_gradient: bool = True):
        """
        If compute_gradient is True, this is a training update, otherwise this is a validation / test pass
        :param batch_dict:
        :param running_loss:
        :param batch_index:
        :param running_metrics:
        :param compute_gradient:
        :return:
        """
        if compute_gradient:
            self.optimizer.zero_grad()

        model_inputs = {inp: batch_dict[inp] for inp in self.model.inputs_names}
        y_pred = self.model(**model_inputs)

        loss_params = {
            "input": y_pred,
            "target": batch_dict['y_target']}

        if hasattr(self.loss.loss, 'mask') and self.mask_index:
            loss_params['mask_index'] = self.mask_index

        loss = self.loss.loss(**loss_params)
        penalty = torch.Tensor([0])
        if hasattr(self, "regularizer"):
            penalty += self.regularizer.regularizer.compute_penalty(model=self.model)

        loss_batch = loss.item() + penalty.item()
        # TODO: see if we can improve the online average (check exponential average)
        running_loss += (loss_batch - running_loss) / (batch_index + 1)

        if compute_gradient:
            loss.backward()  # TODO: See if we want to optimize loss or loss + penalty
            # Gradient clipping
            if hasattr(self, 'gradient_clipping'):
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.gradient_clipping)

            self.optimizer.step()

        for metric in self.metrics.names:
            metric_batch = self.metrics.metrics[metric](**loss_params)
            running_metrics[f"running_{metric}"] += (metric_batch - running_metrics[f"running_{metric}"]) / (batch_index + 1)

        return running_loss, running_metrics


slack_webhook_url = "YourWebhookURL"
slack_channel = "YourFavoriteSlackChannel"


@slack_sender(webhook_url=slack_webhook_url, channel=slack_channel)
def run_with_slack(runner, test_at_the_end: bool = False):
    runner.run(test_at_the_end=test_at_the_end)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="launch an experiment")

    parser.add_argument('--config', type=str)
    args = parser.parse_args()

    args.config = args.config or 'experiments/mlp.json'
    runner = Runner.load_from_project(experiment_file=args.config)

    if slack_webhook_url and slack_webhook_url != "YourWebhookURL":
        run_with_slack(runner=runner, test_at_the_end=True)
    else:
        # runner.run(test_at_the_end=True)
        runner.run_pipeline()
