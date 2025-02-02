import argparse
from datetime import datetime
import math
import numpy as np
import os
import subprocess
import time
import sys
import tensorflow as tf
from tensorflow.python.client import device_lib
import traceback

from datasets.datafeeder import DataFeeder
from hparams import hparams, hparams_debug_string
from models import create_model
from text import sequence_to_text
from util import audio, infolog, plot, ValueWindow, input

log = infolog.log
kb = input.KBHit() #init keyboard listening
keycommand=[]
skp = ""


def get_git_commit():
  subprocess.check_output(['git', 'diff-index', '--quiet', 'HEAD'])   # Verify client is clean
  commit = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()[:10]
  log('Git commit: %s' % commit)
  return commit


def add_stats(model):
  with tf.variable_scope('stats') as scope:
    tf.summary.histogram('linear_outputs', model.linear_outputs)
    tf.summary.histogram('linear_targets', model.linear_targets)
    tf.summary.histogram('mel_outputs', model.mel_outputs)
    tf.summary.histogram('mel_targets', model.mel_targets)
    tf.summary.scalar('loss_mel', model.mel_loss)
    tf.summary.scalar('loss_linear', model.linear_loss)
    tf.summary.scalar('learning_rate', model.learning_rate)
    tf.summary.scalar('loss', model.loss)
    gradient_norms = [tf.norm(grad) for grad in model.gradients]
    tf.summary.histogram('gradient_norm', gradient_norms)
    tf.summary.scalar('max_gradient_norm', tf.reduce_max(gradient_norms))
    return tf.summary.merge_all()


def time_string():
  return datetime.now().strftime('%Y-%m-%d %H:%M')


def train(log_dir, args):
  commit = get_git_commit() if args.git else 'None'
  checkpoint_path = os.path.join(log_dir, 'model.ckpt')
  input_path = os.path.join(args.base_dir, args.input)
  log('Devices available to tensorflow: '+str(device_lib.list_local_devices()))
  log('Checkpoint path: %s' % checkpoint_path)
  log('Loading training data from: %s' % input_path)
  log('Using model: %s' % args.model)
  log(hparams_debug_string())

  # Set up DataFeeder:
  coord = tf.train.Coordinator()
  with tf.variable_scope('datafeeder') as scope:
    feeder = DataFeeder(coord, input_path, hparams)

  # Set up model:
  global_step = tf.Variable(0, name='global_step', trainable=False)
  with tf.variable_scope('model') as scope:
    model = create_model(args.model, hparams)
    model.initialize(feeder.inputs, feeder.input_lengths, feeder.mel_targets, feeder.linear_targets)
    model.add_loss()
    model.add_optimizer(global_step)
    stats = add_stats(model)

  # Bookkeeping:
  step = 0
  time_window = ValueWindow(100)
  loss_window = ValueWindow(100)
  saver = tf.train.Saver(max_to_keep=5, keep_checkpoint_every_n_hours=2)

  # Train!
  with tf.Session() as sess:#config=tf.ConfigProto(log_device_placement=True)) as sess:
    try:
      summary_writer = tf.summary.FileWriter(log_dir, sess.graph)
      sess.run(tf.global_variables_initializer())

      if args.restore_step:
        # Restore from a checkpoint if the user requested it.
        restore_path = '%s-%d' % (checkpoint_path, args.restore_step)
        saver.restore(sess, restore_path)
        log('Resuming from checkpoint: %s at commit: %s' % (restore_path, commit), slack=True)
      else:
        log('Starting new training run at commit: %s' % commit, slack=True)



      feeder.start_in_session(sess)

      while not coord.should_stop():
        start_time = time.time()
        step, loss, opt = sess.run([global_step, model.loss, model.optimize])
        time_window.append(time.time() - start_time)
        loss_window.append(loss)
        message = 'Step %-7d [%.03f sec/step, loss=%.05f, avg_loss=%.05f]' % (
          step, time_window.average, loss, loss_window.average)
        log(message, slack=(step % args.checkpoint_interval == 0))

        commandHelp = False #so commands can't be called twice
        commandExit = False
        commandExitCkpt = False
        commandSummary = False
        commandCkpt = False
        commandAudio = False
        
        command = ""
        global keycommand
        global skp
        if kb.kbhit(): #reads key
          c = kb.getch() #getkey
          if(ord(c) == 10 or ord(c) == 13):
              command = ''.join(str(e) for e in keycommand) #join letters
              print("Command recieved from stdin: "+command)
              keycommand = []
          else:
              keycommand.append(c) #add letter to keycommand
        if (command != ""): #user can feed in stdin to respond to training ;)
          line = command.strip('\n').casefold();
          if (line == "help" and commandHelp == False):
            commandHelp = True
            print("Help for stdin while training:\nInput: 'help', Output: Help string\nInput: 'savecheckpoint', Output: Saves checkpoint of model at its current training step\nInput: 'saveaudio', Output: Saves audio of current data run through model\nInput: 'exit', Output: Requests a clean exit of the training and saves a checkpoint of the current training step\nInput: 'exitnockpt', Output: Requests a clean exit of the training without saving a checkpoint")
          elif (line == "savecheckpoint" and commandCkpt == False):
            commandCkpt = True
            log('Saving checkpoint to: %s-%d (requested by user)' % (checkpoint_path, step))
            saver.save(sess, checkpoint_path, global_step=step)
          elif (line == "saveaudio" and commandAudio == False):
            commandAudio = True
            log('Saving audio and alignment (requested by user)...')
            input_seq, spectrogram, alignment = sess.run([
              model.inputs[0], model.linear_outputs[0], model.alignments[0]])
            waveform = audio.inv_spectrogram(spectrogram.T)
            
            audio_path = os.path.join(log_dir, 'step-%d-audio.wav' % step)
            plot_path = os.path.join(log_dir, 'step-%d-align.png' % step)
            full_checkpoint_path = ('%s-%d' % (checkpoint_path, step))

            audio.save_wav(waveform, audio_path)
            plot.plot_alignment(alignment, plot_path,
             info='%s, %s, %s, step=%d, loss=%.5f' % (args.model, commit, time_string(), step, loss))
            log('Input: %s' % sequence_to_text(input_seq))
            if (args.upload_gdrive != ""):
              log("Uploading to audio, alignment, and log to google drive folder \'"+args.upload_gdrive+"\'from audio path: "+audio_path+", plot path: "+plot_path+", log path: "+os.path.join(log_dir, 'train.log'))
              try:
                subprocess.call([skp,"upload",audio_path,("/"+args.upload_gdrive)])
                subprocess.call([skp,"upload",plot_path,("/"+args.upload_gdrive)])
                subprocess.call([skp,"upload",os.path.join(log_dir, 'train.log'),("/"+args.upload_gdrive)])
                subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.index" % step)),("/"+args.upload_gdrive)])
                subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.meta" % step)),("/"+args.upload_gdrive)])
                subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.data-00000-of-00001" % step)),("/"+args.upload_gdrive)])
              except Exception as e:
                log('Error uploading to google drive due to exception: %s' % e, slack=True)
                traceback.print_exc()
          elif (line == "savesummary" and commandSummary == False):
            commandSummary = True
            log('Writing summary at step: %d (requested by user)' % step)
            summary_writer.add_summary(sess.run(stats), step)
          elif (line == "exit" and commandExitCkpt == False):
            commandExitCkpt = True
            log('Saving checkpoint to: %s-%d (requested by user) and exiting' % (checkpoint_path, step))
            saver.save(sess, checkpoint_path, global_step=step)
            coord.request_stop()
          elif (line == "exitnockpt" and commandExit == False):
            commandExit = True
            log('Exiting training session (requested by user)')
            coord.request_stop()
          else:
            commandHelp = True
            print("Help for stdin while training:\nInput: 'help', Output: Help string\nInput: 'savecheckpoint', Output: Saves checkpoint of model at its current training step\nInput: 'saveaudio', Output: Saves audio of current data run through model\nInput: 'exit', Output: Requests a clean exit of the training and saves a checkpoint of the current training step\nInput: 'exitnockpt', Output: Requests a clean exit of the training without saving a checkpoint")

        if loss > 100 or math.isnan(loss):
          log('Loss exploded to %.05f at step %d!' % (loss, step), slack=True)
          raise Exception('Loss Exploded')

        if step % args.summary_interval == 0:
          log('Writing summary at step: %d' % step)
          summary_writer.add_summary(sess.run(stats), step)

        if step % args.checkpoint_interval == 0:
          log('Saving checkpoint to: %s-%d' % (checkpoint_path, step))
          saver.save(sess, checkpoint_path, global_step=step)
          log('Saving audio and alignment...')
          input_seq, spectrogram, alignment = sess.run([
            model.inputs[0], model.linear_outputs[0], model.alignments[0]])
          waveform = audio.inv_spectrogram(spectrogram.T)

          audio_path = os.path.join(log_dir, 'step-%d-audio.wav' % step)
          plot_path = os.path.join(log_dir, 'step-%d-align.png' % step)
          full_checkpoint_path = ('%s-%d' % (checkpoint_path, step))

          audio.save_wav(waveform, audio_path)
          plot.plot_alignment(alignment, plot_path,
           info='%s, %s, %s, step=%d, loss=%.5f' % (args.model, commit, time_string(), step, loss))
          log('Input: %s' % sequence_to_text(input_seq))
          if (args.upload_gdrive != ""):
            log("Uploading to audio, alignment, and log to google drive folder \'"+args.upload_gdrive+"\'from audio path: "+audio_path+", plot path: "+plot_path+", log path: "+os.path.join(log_dir, 'train.log'))
            try:
              subprocess.call([skp,"upload",audio_path,("/"+args.upload_gdrive)])
              subprocess.call([skp,"upload",plot_path,("/"+args.upload_gdrive)])
              subprocess.call([skp,"upload",os.path.join(log_dir, 'train.log'),("/"+args.upload_gdrive)])
              subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.index" % step)),("/"+args.upload_gdrive)])
              subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.meta" % step)),("/"+args.upload_gdrive)])
              subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.data-00000-of-00001" % step)),("/"+args.upload_gdrive)])
            except Exception as e:
              log('Error uploading to google drive due to exception: %s' % e, slack=True)
              traceback.print_exc()
              kb.set_normal_term()


    except RuntimeError:
      log('One thread took more than coordinator grace period to stop, it is being killed')
    except Exception as e:
      log('Exiting due to exception: %s' % e, slack=True)
      traceback.print_exc()
      coord.request_stop(e)
      kb.set_normal_term()

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--base_dir', default=os.path.expanduser('~/tacotron'))
  parser.add_argument('--input', default='training/train.txt')
  parser.add_argument('--model', default='tacotron')
  parser.add_argument('--name', help='Name of the run. Used for logging. Defaults to model name.')
  parser.add_argument('--hparams', default='',
    help='Hyperparameter overrides as a comma-separated list of name=value pairs')
  parser.add_argument('--restore_step', type=int, help='Global step to restore from checkpoint.')
  parser.add_argument('--summary_interval', type=int, default=100,
    help='Steps between running summary ops.')
  parser.add_argument('--checkpoint_interval', type=int, default=1000,
    help='Steps between writing checkpoints.')
  parser.add_argument('--slack_url', help='Slack webhook URL to get periodic reports.')
  parser.add_argument('--tf_log_level', type=int, default=1, help='Tensorflow C++ log level.')
  parser.add_argument('--git', action='store_true', help='If set, verify that the client is clean.')
  parser.add_argument('--upload_gdrive', default='', help='Upload files to google drive. This command sets the default folder name on google drive to upload to.')
  parser.add_argument('--skicka_path', default='', help='Path to skicka command from command line. Will find automatically if set to none.')
  args = parser.parse_args()
  os.environ['TF_CPP_MIN_LOG_LEVEL'] = str(args.tf_log_level)
  run_name = args.name or args.model
  log_dir = os.path.join(args.base_dir, 'logs-%s' % run_name)
  if not os.geteuid()==0:
    raise Exception("You must be root to run this script, please start with sudo")
  os.makedirs(log_dir, exist_ok=True)
  infolog.init(os.path.join(log_dir, 'train.log'), run_name, args.slack_url)
  hparams.parse(args.hparams)
  global skp
  if (args.upload_gdrive != ""):
    if (args.skicka_path == ""): #/home/aaron/.go//bin/skicka
      print("Determining skicka path...")
      proc = subprocess.Popen(['which', 'skicka'], stdout=subprocess.PIPE)
      output = proc.stdout.read()
      sliceend = str(output[0:len(output)-1])
      skp = str(sliceend[2:len(sliceend)-1])
      if (len(skp) < 1):
        raise Exception('Skicka path was not determined from this script. Please specify a path by using the --skicka_path flag.')
        kb.set_normal_term()
    else:
      skp = args.skicka_path
    print("Skicka path: "+skp)
    if (subprocess.getstatusoutput([skp])[0] == 0):
      log('Using google drive upload in run')
      print("Initializing skicka...")
      subprocess.call([skp,"init"])
      print("Please enter google username and password")
      subprocess.call([skp,"ls"])
      print("Listed files in your drive successfully.")
      print("Making directories in drive...")
      subprocess.call([skp,"mkdir","/"+args.upload_gdrive])
      if (args.restore_step):
        print("Uploading current files from restore_step")
        step = args.restore_step
        audio_path = os.path.join(log_dir, 'step-%d-audio.wav' % step)
        plot_path = os.path.join(log_dir, 'step-%d-align.png' % step)
        subprocess.call([skp,"upload",audio_path,("/"+args.upload_gdrive)])
        subprocess.call([skp,"upload",plot_path,("/"+args.upload_gdrive)])
        subprocess.call([skp,"upload",os.path.join(log_dir, 'train.log'),("/"+args.upload_gdrive)])
        subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.index" % step)),("/"+args.upload_gdrive)])
        subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.meta" % step)),("/"+args.upload_gdrive)])
        subprocess.call([skp,"upload",os.path.join(log_dir, ("model.ckpt-%d.data-00000-of-00001" % step)),("/"+args.upload_gdrive)])
    else:
      raise Exception('If --upload_gdrive is set to a value, you must have the tool \'skicka\' installed. Run drivesetup.sh to install it.')
      kb.set_normal_term()

  train(log_dir, args)


if __name__ == '__main__':
  main()
  kb.set_normal_term()
