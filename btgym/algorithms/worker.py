#
# Original A3C code comes from OpenAI repository under MIT licence:
# https://github.com/openai/universe-starter-agent
#
# Papers:
# https://arxiv.org/abs/1602.01783
# https://arxiv.org/abs/1611.05397

import sys
sys.path.insert(0,'..')

import os
import logging
import multiprocessing

import cv2
import tensorflow as tf


class FastSaver(tf.train.Saver):
    """
    Disables write_meta_graph argument,
    which freezes entire process and is mostly useless.
    """
    def save(self,
             sess,
             save_path,
             global_step=None,
             latest_filename=None,
             meta_graph_suffix="meta",
             write_meta_graph=True):
        super(FastSaver, self).save(sess,
                                    save_path,
                                    global_step,
                                    latest_filename,
                                    meta_graph_suffix,
                                    False)

class Worker(multiprocessing.Process):
    """
    Distributed tf worker class.

    Sets up environment, trainer and starts training process in supervised session.
    """
    env = None

    def __init__(self,
                 env_config,
                 policy_config,
                 trainer_config,
                 cluster_spec,
                 job_name,
                 task,
                 log_dir,
                 log,
                 log_level,
                 max_train_steps,
                 random_seed=None,
                 test_mode=False):
        """

        Args:
            env_config:     environment class_config_dict.
            policy_config:  model policy estimator class_config_dict.
            trainer_config: algorithm class_config_dict.
            cluster_spec:   tf.cluster specification.
            job_name:       worker or parameter server.
            task:           integer number, 0 is chief worker.
            log_dir:        for tb summaries and checkpoints.
            log:            parent logger
            log_level:      --
            max_steps:      number of train steps
            test_mode:      if True - use Atari mode, BTGym otherwise.
        """
        super(Worker, self).__init__()
        self.env_class = env_config['class_ref']
        self.env_kwargs = env_config['kwargs']
        self.policy_config = policy_config
        self.trainer_class = trainer_config['class_ref']
        self.trainer_kwargs = trainer_config['kwargs']
        self.cluster_spec = cluster_spec
        self.job_name = job_name
        self.task = task
        self.log_dir = log_dir
        self.max_train_steps = max_train_steps
        self.log = log
        logging.basicConfig()
        self.log = logging.getLogger('{}_{}'.format(self.job_name, self.task))
        self.log.setLevel(log_level)
        self.test_mode = test_mode
        self.random_seed = random_seed

    def run(self):
        """Worker runtime body.
        """
        tf.reset_default_graph()

        if self.test_mode:
            import gym

        # Define cluster:
        cluster = tf.train.ClusterSpec(self.cluster_spec).as_cluster_def()

        # Start tf.server:
        if self.job_name in 'ps':
            server = tf.train.Server(
                cluster,
                job_name=self.job_name,
                task_index=self.task,
                config=tf.ConfigProto(device_filters=["/job:ps"])
            )
            self.log.debug('parameters_server started.')
            # Just block here:
            server.join()

        else:
            server = tf.train.Server(
                cluster,
                job_name='worker',
                task_index=self.task,
                config=tf.ConfigProto(
                    intra_op_parallelism_threads=1,  # original was: 1
                    inter_op_parallelism_threads=2  # original was: 2
                )
            )
            self.log.debug('worker_{} tf.server started.'.format(self.task))

            self.log.debug('making environment.')
            if not self.test_mode:
                # Assume BTgym env. class:
                self.log.debug('worker_{} is data_master: {}'.format(self.task, self.env_kwargs['data_master']))
                try:
                    self.env = self.env_class(**self.env_kwargs)

                except:
                    raise SystemExit(' Worker_{} failed to make BTgym environment'.format(self.task))

            else:
                # Assume atari testing:
                try:
                    self.env = self.env_class(self.env_kwargs['gym_id'])

                except:
                    raise SystemExit(' Worker_{} failed to make Atari Gym environment'.format(self.task))

            self.log.debug('worker_{}:envronment ok.'.format(self.task))
            # Define trainer:
            trainer = self.trainer_class(
                env=self.env,
                task=self.task,
                policy_config=self.policy_config,
                log=self.log,
                random_seed=self.random_seed,
                **self.trainer_kwargs,
            )

            self.log.debug('worker_{}:trainer ok.'.format(self.task))

            # Saver-related:
            variables_to_save = [v for v in tf.global_variables() if not v.name.startswith("local")]
            init_op = tf.variables_initializer(variables_to_save)
            init_all_op = tf.global_variables_initializer()

            saver = FastSaver(variables_to_save)

            self.log.debug('worker_{}: vars_to_save:'.format(self.task))
            for v in variables_to_save:
                self.log.debug('{}: {}'.format(v.name, v.get_shape()))

            def init_fn(ses):
                self.log.debug("Initializing all parameters.")
                ses.run(init_all_op)

            config = tf.ConfigProto(device_filters=["/job:ps", "/job:worker/task:{}/cpu:0".format(self.task)])
            logdir = os.path.join(self.log_dir, 'train')
            summary_dir = logdir + "_{}".format(self.task)

            summary_writer = tf.summary.FileWriter(summary_dir)

            sv = tf.train.Supervisor(
                is_chief=(self.task == 0),
                logdir=logdir,
                saver=saver,
                summary_op=None,
                init_op=init_op,
                init_fn=init_fn,
                ready_op=tf.report_uninitialized_variables(variables_to_save),
                global_step=trainer.global_step,
                save_model_secs=300,
            )
            self.log.debug("worker_{}: connecting to the parameter server... ".format(self.task))

            with sv.managed_session(server.target, config=config) as sess, sess.as_default():
                sess.run(trainer.sync)
                trainer.start(sess, summary_writer)
                global_step = sess.run(trainer.global_step)
                # Fill replay memory, if any:
                if hasattr(trainer,'memory'):
                    if not trainer.memory.is_full():
                        trainer.memory.fill()

                self.log.warning("worker_{}: started training at step: {}".format(self.task, global_step))
                while not sv.should_stop() and global_step < self.max_train_steps:
                    trainer.process(sess)
                    global_step = sess.run(trainer.global_step)

                # Ask for all the services to stop:
                self.env.close()
                sv.stop()
            self.log.warning('worker_{}: reached {} steps, exiting.'.format(self.task, global_step))



