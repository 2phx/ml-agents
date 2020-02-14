import logging
import numpy as np
from typing import Any, Dict, Optional, List

from mlagents.tf_utils import tf

from mlagents_envs.timers import timed
from mlagents_envs.base_env import BatchedStepResult
from mlagents.trainers.brain import BrainParameters
from mlagents.trainers.models import EncoderType
from mlagents.trainers.models import LearningModel
from mlagents.trainers.tf_policy import TFPolicy
from mlagents.trainers.common.distributions import (
    GaussianDistribution,
    TanhSquashedGaussianDistribution,
)

logger = logging.getLogger("mlagents.trainers")

EPSILON = 1e-6  # Small value to avoid divide by zero


class NNPolicy(TFPolicy):
    def __init__(
        self,
        seed: int,
        brain: BrainParameters,
        trainer_params: Dict[str, Any],
        is_training: bool,
        load: bool,
        tanh_squash: bool = False,
        resample: bool = False,
        create_tf_graph: bool = True,
    ):
        """
        Policy that uses a multilayer perceptron to map the observations to actions. Could
        also use a CNN to encode visual input prior to the MLP. Supports discrete and
        continuous action spaces, as well as recurrent networks.
        :param seed: Random seed.
        :param brain: Assigned BrainParameters object.
        :param trainer_params: Defined training parameters.
        :param is_training: Whether the model should be trained.
        :param load: Whether a pre-trained model will be loaded or a new one created.
        :param tanh_squash: Whether to use a tanh function on the continuous output, or a clipped output.
        :param resample: Whether we are using the resampling trick to update the policy in continuous output.
        """
        super().__init__(seed, brain, trainer_params, load)
        self.tf_optimizer: Optional[tf.train.Optimizer] = None
        self.grads = None
        self.update_batch: Optional[tf.Operation] = None
        num_layers = trainer_params["num_layers"]
        self.h_size = trainer_params["hidden_units"]
        if num_layers < 1:
            num_layers = 1
        self.num_layers = num_layers
        self.vis_encode_type = EncoderType(
            trainer_params.get("vis_encode_type", "simple")
        )
        self.tanh_squash = tanh_squash
        self.resample = resample
        self.trainable_variables: List[tf.Variable] = []

        # Non-exposed parameters; these aren't exposed because they don't have a
        # good explanation and usually shouldn't be touched.
        self.log_std_min = -20
        self.log_std_max = 2
        if create_tf_graph:
            self.create_tf_graph()

    def get_trainable_variables(self) -> List[tf.Variable]:
        """
        Returns a List of the trainable variables in this policy. if create_tf_graph hasn't been called,
        returns empty list.
        """
        return self.trainable_variables

    def create_tf_graph(self) -> None:
        """
        Builds the tensorflow graph needed for this policy.
        """
        with self.graph.as_default():
            _vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
            if len(_vars) > 0:
                # We assume the first thing created in the graph is the Policy. If
                # already populated, don't create more tensors.
                return

            self.create_input_placeholders()
            if self.use_continuous_act:
                self._create_cc_actor(
                    self.h_size,
                    self.num_layers,
                    self.vis_encode_type,
                    self.tanh_squash,
                    self.resample,
                )
            else:
                self._create_dc_actor(
                    self.h_size, self.num_layers, self.vis_encode_type
                )
            self.trainable_variables = tf.get_collection(
                tf.GraphKeys.TRAINABLE_VARIABLES, scope="policy"
            )
            self.trainable_variables += tf.get_collection(
                tf.GraphKeys.TRAINABLE_VARIABLES, scope="lstm"
            )  # LSTMs need to be root scope for Barracuda export

        self.inference_dict: Dict[str, tf.Tensor] = {
            "action": self.output,
            "log_probs": self.all_log_probs,
            "entropy": self.entropy,
        }
        if self.use_continuous_act:
            self.inference_dict["pre_action"] = self.output_pre
        if self.use_recurrent:
            self.inference_dict["memory_out"] = self.memory_out

        # We do an initialize to make the Policy usable out of the box. If an optimizer is needed,
        # it will re-load the full graph
        self._initialize_graph()

    @timed
    def evaluate(
        self, batched_step_result: BatchedStepResult, global_agent_ids: List[str]
    ) -> Dict[str, Any]:
        """
        Evaluates policy for the agent experiences provided.
        :param batched_step_result: BatchedStepResult object containing inputs.
        :param global_agent_ids: The global (with worker ID) agent ids of the data in the batched_step_result.
        :return: Outputs from network as defined by self.inference_dict.
        """
        feed_dict = {
            self.batch_size_ph: batched_step_result.n_agents(),
            self.sequence_length_ph: 1,
        }
        if self.use_recurrent:
            if not self.use_continuous_act:
                feed_dict[self.prev_action] = self.retrieve_previous_action(
                    global_agent_ids
                )
            feed_dict[self.memory_in] = self.retrieve_memories(global_agent_ids)
        feed_dict = self.fill_eval_dict(feed_dict, batched_step_result)
        run_out = self._execute_model(feed_dict, self.inference_dict)
        return run_out

    def _create_cc_actor(
        self,
        h_size: int,
        num_layers: int,
        vis_encode_type: EncoderType,
        tanh_squash: bool = False,
        resample: bool = False,
    ) -> None:
        """
        Creates Continuous control actor-critic model.
        :param h_size: Size of hidden linear layers.
        :param num_layers: Number of hidden linear layers.
        :param vis_encode_type: Type of visual encoder to use if visual input.
        :param tanh_squash: Whether to use a tanh function, or a clipped output.
        :param resample: Whether we are using the resampling trick to update the policy.
        """
        with tf.variable_scope("policy"):
            hidden_stream = LearningModel.create_observation_streams(
                self.visual_in,
                self.processed_vector_in,
                1,
                h_size,
                num_layers,
                vis_encode_type,
            )[0]

        if self.use_recurrent:
            self.memory_in = tf.placeholder(
                shape=[None, self.m_size], dtype=tf.float32, name="recurrent_in"
            )
            hidden_policy, memory_policy_out = LearningModel.create_recurrent_encoder(
                hidden_stream,
                self.memory_in,
                self.sequence_length_ph,
                name="lstm_policy",
            )

            self.memory_out = tf.identity(memory_policy_out, name="recurrent_out")
        else:
            hidden_policy = hidden_stream

        with tf.variable_scope("policy"):
            if not tanh_squash:
                distribution = GaussianDistribution(
                    hidden_policy, self.act_size, pass_gradients=resample
                )
            else:
                distribution = TanhSquashedGaussianDistribution(
                    hidden_policy, self.act_size, pass_gradients=resample
                )

        if tanh_squash:
            self.output_pre = distribution.sample
            self.output = tf.identity(self.output_pre, name="action")
        else:
            self.output_pre = distribution.sample
            # Clip and scale output to ensure actions are always within [-1, 1] range.
            output_post = tf.clip_by_value(self.output_pre, -3, 3) / 3
            self.output = tf.identity(output_post, name="action")

        self.selected_actions = tf.stop_gradient(self.output)

        self.all_log_probs = tf.identity(
            distribution.total_log_probs, name="action_probs"
        )
        self.entropy = distribution.entropy

        # We keep these tensors the same name, but use new nodes to keep code parallelism with discrete control.
        self.log_probs = self.all_log_probs

        self.action_holder = tf.placeholder(
            shape=[None, self.act_size[0]], dtype=tf.float32, name="action_holder"
        )

    def _create_dc_actor(
        self, h_size: int, num_layers: int, vis_encode_type: EncoderType
    ) -> None:
        """
        Creates Discrete control actor-critic model.
        :param h_size: Size of hidden linear layers.
        :param num_layers: Number of hidden linear layers.
        :param vis_encode_type: Type of visual encoder to use if visual input.
        """
        with tf.variable_scope("policy"):
            hidden_stream = LearningModel.create_observation_streams(
                self.visual_in,
                self.processed_vector_in,
                1,
                h_size,
                num_layers,
                vis_encode_type,
            )[0]

        if self.use_recurrent:
            self.prev_action = tf.placeholder(
                shape=[None, len(self.act_size)], dtype=tf.int32, name="prev_action"
            )
            prev_action_oh = tf.concat(
                [
                    tf.one_hot(self.prev_action[:, i], self.act_size[i])
                    for i in range(len(self.act_size))
                ],
                axis=1,
            )
            hidden_policy = tf.concat([hidden_stream, prev_action_oh], axis=1)

            self.memory_in = tf.placeholder(
                shape=[None, self.m_size], dtype=tf.float32, name="recurrent_in"
            )
            hidden_policy, memory_policy_out = LearningModel.create_recurrent_encoder(
                hidden_policy,
                self.memory_in,
                self.sequence_length_ph,
                name="lstm_policy",
            )

            self.memory_out = tf.identity(memory_policy_out, "recurrent_out")
        else:
            hidden_policy = hidden_stream

        policy_branches = []
        with tf.variable_scope("policy"):
            for size in self.act_size:
                policy_branches.append(
                    tf.layers.dense(
                        hidden_policy,
                        size,
                        activation=None,
                        use_bias=False,
                        kernel_initializer=LearningModel.scaled_init(0.01),
                    )
                )

        raw_log_probs = tf.concat(policy_branches, axis=1, name="action_probs")

        self.action_masks = tf.placeholder(
            shape=[None, sum(self.act_size)], dtype=tf.float32, name="action_masks"
        )
        output, self.action_probs, normalized_logits = LearningModel.create_discrete_action_masking_layer(
            raw_log_probs, self.action_masks, self.act_size
        )

        self.output = tf.identity(output)
        self.all_log_probs = tf.identity(normalized_logits, name="action")

        self.action_holder = tf.placeholder(
            shape=[None, len(policy_branches)], dtype=tf.int32, name="action_holder"
        )
        self.action_oh = tf.concat(
            [
                tf.one_hot(self.action_holder[:, i], self.act_size[i])
                for i in range(len(self.act_size))
            ],
            axis=1,
        )
        self.selected_actions = tf.stop_gradient(self.action_oh)

        action_idx = [0] + list(np.cumsum(self.act_size))

        self.entropy = tf.reduce_sum(
            (
                tf.stack(
                    [
                        tf.nn.softmax_cross_entropy_with_logits_v2(
                            labels=tf.nn.softmax(
                                self.all_log_probs[:, action_idx[i] : action_idx[i + 1]]
                            ),
                            logits=self.all_log_probs[
                                :, action_idx[i] : action_idx[i + 1]
                            ],
                        )
                        for i in range(len(self.act_size))
                    ],
                    axis=1,
                )
            ),
            axis=1,
        )

        self.log_probs = tf.reduce_sum(
            (
                tf.stack(
                    [
                        -tf.nn.softmax_cross_entropy_with_logits_v2(
                            labels=self.action_oh[:, action_idx[i] : action_idx[i + 1]],
                            logits=normalized_logits[
                                :, action_idx[i] : action_idx[i + 1]
                            ],
                        )
                        for i in range(len(self.act_size))
                    ],
                    axis=1,
                )
            ),
            axis=1,
            keepdims=True,
        )
