
import json
import logging
import os
import time
import uuid

import nltk
import rq

from crackq import crackqueue, hash_modes, run_hashcat, auth
from crackq.conf import hc_conf
from flask import (Flask, redirect, request, session, make_response, url_for)
from flask_restful import reqparse, abort, Resource
from logging.config import fileConfig
from marshmallow import Schema, fields, validate, ValidationError
from marshmallow.validate import Length, Range, Regexp
from operator import itemgetter
from pathlib import Path
from pypal import pypal
from redis import Redis
from rq import use_connection, Queue
from saml2 import BINDING_HTTP_POST
from saml2 import BINDING_HTTP_REDIRECT
from saml2 import sigver

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_required, login_user, logout_user, UserMixin, current_user
from flask_session import Session
from crackq.models import User
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy import create_engine, Column, ForeignKey
from sqlalchemy.orm import relationship, backref
from sqlalchemy.types import (
    Boolean,
    DateTime,
    Integer,
    String,
    TypeDecorator,
    JSON,
    )

from sqlalchemy.ext.declarative import declarative_base
#from crackq import create_app
from crackq.db import db
import crackq
#from crackq import app
#from wsgi import db, app
#import crackq
#from wsgi import app

# set perms
os.umask(0o077)

#Setup logging
fileConfig('log_config.ini')
logger = logging.getLogger()
login_manager = LoginManager()

app = Flask(__name__)

#session = Session(app)
#session.app.session_interface.db.create_all()
#db.create_all()


CRACK_CONF = hc_conf()

class StringContains(validate.Regexp):
    """
    Custom validation class to reject any strtings matching supplied regex

    See validate.Regexp for args/return values
    """
    default_message = 'Invalid input for this field.'

    def __call__(self, value):
        if len(self.regex.findall(value)) > 0:
            raise ValidationError(self._format_error(value))
            return value


class parse_json_schema(Schema):
    """
    Class to create the schema for parsing received JSON arguments

    job_details: str
                string returned from rq.job.description

    Returns
    ------
    deets_dict: dictionary
                only the specified job details are returned

    """
    error_messages = {
            "name": "Invalid input characters",
            "username": "Invalid input characters",
            }
    job_id = fields.UUID(allow_none=False)# validate=Length(min=1, max=32))
    batch_job = fields.List(fields.Dict(fields.UUID(), fields.Int(min=0, max=1000)))
    place = fields.Int(validate=Range(min=1, max=100))
    hash_list = fields.List(fields.String(validate=StringContains(r'[^A-Za-z0-9\*\$\@\/\\\.\:\-\_\+\.]+\~')),
                            allow_none=True, error_messages=error_messages)
    wordlist = fields.Str(allow_none=True, validate=[StringContains(r'[\W]\-'),
                                                     Length(min=1, max=60)])
    attack_mode = fields.Int(allow_none=True, validate=Range(min=0, max=7))
    rules = fields.List(fields.String(validate=[StringContains(r'[\W]\-'),
                                                Length(min=1, max=60)]),
                        allow_none=True)
    username = fields.Bool(allow_none=True)
    disable_brain = fields.Bool(allow_none=True)
    mask = fields.Str(allow_none=True, validate=StringContains(r'[^aldsu\?0-9a-zA-Z]'))
    mask_file = fields.List(fields.String(validate=[StringContains(r'[\W]\-'),
                                                Length(min=1, max=60)]),
                        allow_none=True)
    name = fields.Str(allow_none=True, validate=StringContains('[\W]'), error_messages=error_messages)
    hash_mode = fields.Int(allow_none=False, validate=Range(min=0, max=65535))
    restore = fields.Int(validate=Range(min=0, max=1000000000000))
    user = fields.Str(allow_none=False, validate=StringContains(r'[\W]'))
    password = fields.Str(allow_none=False, validate=StringContains(r'[^\w\!\@\#\$\%\^\&\*\(\)\-\+\.\,\\\/]'))


def get_jobdetails(job_details):
    """
    Function to help pull only required information from a specified redis job
    description string.
    job_details: str
                string returned from rq.job.description

    Returns
    ------
    deets_dict: dictionary
                only the specified job details are returned

    """
    deets_dict = {}
    deet_match_list = [
                    'hash_mode',
                    'attack_mode',
                    'mask',
                    'wordlist',
                    'rules',
                    'name',
                    'username',
                    'disable_brain',
                    'restore']
    ###***make this less ugly
    ###***review stripping here for improvement
    #review rules processing
    # Process rules list separately as workaround for splitting on comma
    if '[' in job_details:
        ###***add mask_file here when updating to allow list of files
        rules_split = job_details[job_details.rfind('[')+1:job_details.rfind(']')].strip()
        rules_list = [rule.strip().rstrip("'").lstrip("'") for rule in rules_split.split(',')]
    else:
        rules_list = None
    deets_split = job_details[job_details.rfind('(')+1:job_details.rfind(')')].split(',')
    for deets in deets_split:
        deet = deets.split('=')[0].strip(' ')
        if deet in deet_match_list:
            deets_dict[deet] = deets.strip().split('=')[1].strip().rstrip("'").lstrip("'")
    if rules_list and rules_list != '':
        ###***move to multi-line?
        deets_dict['rules'] = [list(
            CRACK_CONF['rules'].keys())[rules_list.index(rule)] for rule in rules_list]
    else:
        deets_dict['rules'] = None
    if deets_dict['mask'] and deets_dict['mask'] != '':
        mask = deets_dict['mask']
        for key, mask_file in dict(CRACK_CONF['masks']).items():
            if mask in mask_file:
                deets_dict['mask'] = key
            else:
                deets_dict['masks'] = None
    if deets_dict['wordlist'] != 'None' and deets_dict['wordlist'] != '':
        wordlist = deets_dict['wordlist']
        deets_dict['wordlist'] = list(
            CRACK_CONF['wordlists'].keys())[list(
                CRACK_CONF['wordlists'].values()
                ).index(wordlist)]

    return deets_dict


def add_jobid(job_id):
    """Add job_id to job_ids column in user table"""
    user = User.query.filter_by(username=current_user.username).first()
    if user.job_ids:
        logger.debug('Current registered job_ids: {}'.format(user.job_ids))
        jobs = json.loads(user.job_ids)
    else:
        logger.debug('No job_ids registered with current user')
        jobs = None
    logger.info('Registering new job_id to current user: {}'.format(job_id))
    if isinstance(jobs, list):
        if job_id not in jobs:
            jobs.append(job_id)
        else:
            logger.warning('job_id already registered to user: {}'.format(job_id))
    else:
        jobs = [job_id]
    user.job_ids = json.dumps(jobs)
    db.session.commit()
    logger.debug('user.job_ids: {}'.format(user.job_ids))


def del_jobid(job_id):
    """Delete job_id from job_ids column in user table"""
    user = User.query.filter_by(username=current_user.username).first()
    if user.job_ids:
        jobs = json.loads(user.job_ids)
        logger.debug('Registered jobs: {}'.format(jobs))
    else:
        logger.debug('No job_ids registered with current user')
        return False
    if isinstance(jobs, list):
        logger.info('Unregistering job_id: {}'.format(job_id))
        if job_id in jobs:
            jobs.remove(job_id)
        else:
            return False
    else:
        logger.error('Error removing job_id')
        return False
    user.job_ids = json.dumps(jobs)
    db.session.commit()
    logger.debug('user.job_ids: {}'.format((user.job_ids)))
    return True


def check_jobid(job_id):
    """Check user owns the job_id"""
    logger.debug('Checking job_id: {} belongs to user: {}'.format(
                job_id, current_user.username))
    user = User.query.filter_by(username=current_user.username).first()
    if user.job_ids:
        if job_id in user.job_ids:
            return True
        else:
            return False
    else:
        return False


def create_user(username):
    if User.query.filter_by(username=username).first():
        logger.debug('User already exists')
        return False
    else:
        user = User(username=username)
        db.session.add(user)
        db.session.commit()
        logger.debug('New user added')
        return True

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)

class Sso(Resource):
    """
    SAML2 Single Sign On Class

    Login class handles saml sso authentication responses from IDP,
    validates authenticity and authenticates if successful.
    """
    def __init__(self):
        if CRACK_CONF['auth']['type'] == 'saml2':
            self.meta_url = CRACK_CONF['auth']['saml_manifest']
            self.meta_file = CRACK_CONF['auth']['meta_file']
            self.entity_id = CRACK_CONF['auth']['entity_id']
            self.group = CRACK_CONF['auth']['group']
            self.saml_auth = auth.Saml2(self.meta_url, self.meta_file, self.entity_id)
            self.saml_client = self.saml_auth.s_client()
            #self.reqid = None

    def get(self):
        """
        Login mechanism, using GET to redirect to SAML IDP.
        """
        ###***validate
        if CRACK_CONF['auth']['type'] == 'saml2':
            self.reqid, info = self.saml_client.prepare_for_authenticate()
            redirect_url = None
            for key, value in info['headers']:
                if key is 'Location':
                    redirect_url = value
            response = redirect(redirect_url, code=302)
            return response
        else:
            return 'Method not supported', 405

    def post(self):
        """
        Handle returned SAML reponse
        """
        if not CRACK_CONF['auth']['type'] == 'saml2':
            return 'Method not supported', 405
        ###***validate
        ###***readd/fix reqid verification
        saml_resp = request.form['SAMLResponse']
        logger.debug('SAML SSO reponse received:\n {}'.format(saml_resp))
        try:
            saml_parse = self.saml_client.parse_authn_request_response(saml_resp,
                                                        BINDING_HTTP_POST)
        except sigver.SignatureError as err:
            return 'Invalid Signature', 500
        except saml2.validate.ResponseLifetimeExceed as err:
            return 'Invalid SAML Request', 500
        #if saml_parse.in_response_to not in self.reqid:
        #    'Unsolicited authentication response', 500
        if saml_parse.authn_statement_ok():
            user_info = saml_parse.ava.items()
            groups = []
            for key, val in user_info:
                if 'name' in key:
                    username = val[0]
                if self.group and 'Group' in key:
                    groups = val
            if self.group:
                if len(groups) > 0:
                    if self.group not in groups:
                        logger.info('User authorised, but not in valid domain group')
                        return 'User is not authorised to use this service', 401
                else:
                    logger.info('No groups returned in SAML response')
                    return 'User is not authorised to use this service', 401
            try:
                username
            except UnboundLocalError:
                return json.dumps({'msg': 'No user returned in SAML response'}), 500
            logging.info('Authenticated: {}'.format(username))
            user = load_user(username)
            if user:
                crackq.app.session_interface.regenerate(session)
                login_user(user)
            else:
                create_user(username)
                user = load_user(username)
            if isinstance(user, User):
                crackq.app.session_interface.regenerate(session)
                login_user(user)
            else:
                logging.error('No user object loaded')
                return json.dumps({"msg": "Bad username or password"}), 401
            #return 'OK', 200
            #return redirect(request.url_root)
            ###*** temp fix, set the host url dynamically
            #return redirect('https://crackq.xxx.com/')
            return redirect('/', _external=True)
        else:
            logger.info('Login error: {}'.format(authn))
            return json.dumps({"msg": "Bad username or password"}), 401


class Login(Resource):
    """
    Authentication handler

    Login class handles authentication, it's protocol agnostic
    and just needs the 'authenticate' fucntion to provide a
    'Success' or 'Failure' result. The 'authenticate' function
    can use any supported protocols or a custom protocol can be
    created.
    """
    def post(self):
        """
        Login mechanism, using POST.
        Supply the following in the body: {"user": "xxx", "password": "xxx"}

        """
        marsh_schema = parse_json_schema().loads(json.dumps(request.json))
        if len(marsh_schema.errors) > 0:
            logger.debug('Validation error: {}'.format(marsh_schema.errors))
            return marsh_schema.errors, 500
        else:
            args = marsh_schema.data
        if CRACK_CONF['auth']['type'] == 'ldap':
            username = args['user']
            password = args['password']
            if not username:
                return json.dumps({"msg": "Missing username parameter"}), 400
            if not password:
                return json.dumps({"msg": "Missing password parameter"}), 400
            ldap_uri = CRACK_CONF['auth']['ldap_server']
            authn = auth.Ldap.authenticate(ldap_uri, username, password)
            if authn is "Success":
                logging.info('Authenticated: {}'.format(username))
                user = load_user(username)
                if user:
                    crackq.app.session_interface.regenerate(session)
                    login_user(user)
                else:
                    create_user(username)
                    user = load_user(username)
                if isinstance(user, User):
                    crackq.app.session_interface.regenerate(session)
                    login_user(user)
                else:
                    logging.error('No user object loaded')
                    return json.dumps({"msg": "Bad username or password"}), 401
                return 'OK', 200
                #return redirect('/queuing/all')
            elif authn is "Invalid Credentials":
                return json.dumps({"msg": "Bad username or password"}), 401
            else:
                logger.info('Login error: {}'.format(authn))
                return json.dumps({"msg": "Bad username or password"}), 401
        else:
            return 'Method not supported', 405


class Logout(Resource):
    """
    Session Logout

    Class to logout and clear flask session cookie
    """
    @login_required
    def get(self):
        logger.info('User logged out: {}'.format(current_user.username))
        user = User.query.filter_by(username=current_user.username).first()
        #sid = request.cookies.get(app.session_cookie_name)
        sid = request.cookies.get(crackq.app.session_cookie_name)
        crackq.app.session_interface.destroy(session)
        logout_user()
        return 'Logged Out', 200


class Queuing(Resource):
    """
    Class to interact with the crackqueue module

    This will instantiate a crackqueue instance and use
    it to manage jobs in the Redis queue using RQ

    """
    def __init__(self):
        self.crack_q = crackqueue.Queuer()
        self.q = self.crack_q.q_connect()
        self.crack = run_hashcat.Crack()
        rconf = CRACK_CONF['redis']
        self.log_dir = CRACK_CONF['files']['log_dir']
        #self.redis_con = Redis()
        self.redis_con = Redis(rconf['host'], rconf['port'])
                               # password=rconf['password'])
        self.req_max = CRACK_CONF['misc']['req_max']
        #self.report_dir = CRACK_CONF['reports']['dir']

    def zombie_check(self, started, failed, cur_list):
        """
        This method will check and remove zombie jobs from
        the started queue.

        RQ has a bug which causes multiple started jobs to exist
        after a system error has occured (unplanned exeception of some sort).
        This method will clean this up and requeus the affected job.
        """
        logger.debug('Checking for zombie jobs')
        while len(started.get_job_ids()) > 1:
            logger.debug('Zombie job detected')
            logger.debug('Started jobs: {}'.format(cur_list))
            hung_dict = {}
            for j in cur_list:
                job = self.q.fetch_job(j)
                if job is not None:
                    hung_dict[j] = job.started_at
            latest = max(hung_dict, key=hung_dict.get)
            for j in cur_list:
                if j != latest:
                    job = self.q.fetch_job(j)
                    if job:
                        job.set_status('failed')
                        failed.add(job)
                        logger.debug('Cleaning state job: {}'.format(j))
                        started.remove(job)
                        try:
                            if job.meta['Requeue Count'] <= int(self.req_max):
                                failed.requeue(j)
                                job.meta['Requeue Count'] += 1
                                job.save_meta()
                        except KeyError:
                            job.meta['Requeue Count'] = 0

    @login_required
    def get(self, job_id):
        """
        Method to get job status

        job_id: str
            hex reprisentation of uuid job ID

        Returns
        ------

        """
        ###***clean this up, maybe remove crackqueue.py entirely?
        ###***re-add this for validation?
        #args = marsh_schema.data
        started = rq.registry.StartedJobRegistry('default',
                                                 connection=self.redis_con)
        failed = rq.registry.FailedJobRegistry('default',
                                               connection=self.redis_con)
        #failed = get_failed_queue(connection=self.redis_con)
        cur_list = started.get_job_ids()
        ###**update all connections to user get_current_connection()??
        self.zombie_check(started, failed, cur_list)
        q_dict = self.crack_q.q_monitor(self.q)
        logger.debug('Current jobs: {}'.format(cur_list))
        failed_dict = self.crack_q.check_failed()
        comp_list = self.crack_q.check_complete()
        last_comp = []

        if len(comp_list) > 0:
            for j in comp_list:
                if check_jobid(j):
                    job = self.q.fetch_job(j)
                    try:
                        job_name = get_jobdetails(job.description)['name']
                    except KeyError:
                        job_name = 'No name'
                    except AttributeError:
                        job_name = 'No name'
                    last_comp = [{ 'job_name': job_name, 'job_id': j}]
        else:
            q_dict['Last Complete'] = []
        q_dict['Last Complete'] = last_comp
        logger.debug('Completed jobs: {}'.format(comp_list))
        logger.debug('q_dict: {}'.format(q_dict))
        ###***check for race conditions here!!
        ###***apply validation here
        ###***fix this shit up, it's messy
        if job_id == 'all':
            ###***definitely make these a function
            if len(cur_list) > 0:
                try:
                    job = self.q.fetch_job(cur_list[0])
                    if job:
                        if isinstance(job.meta['HC State'], dict):
                            job_details = get_jobdetails(job.description)
                            q_dict['Current Job'][cur_list[0]]['Job Details'] = job_details
                    if isinstance(q_dict, dict):
                        if isinstance(q_dict['Current Job'][cur_list[0]]['State']['HC State'], dict):
                            del q_dict['Current Job'][cur_list[0]]['State']['HC State']['Cracked']
                        else:
                            logger.debug('Still initializing')
                    else:
                        logger.error('No Queue')
                except KeyError as err:
                    logger.error('Cant clear cracked yet1: {}'.format(err))
                if len(q_dict) > 0:
                    for qjob_id in q_dict['Queued Jobs']:
                        job = self.q.fetch_job(qjob_id)
                        job_details = get_jobdetails(job.description)
                        q_dict['Queued Jobs'][qjob_id]['Job Details'] = job_details
                        try:
                            if isinstance(q_dict, dict):
                                if isinstance(q_dict['Queued Jobs'][qjob_id]['State']['HC State'], dict):
                                    if 'Cracked' in q_dict['Queued Jobs'][qjob_id]['State']['HC State']:
                                        del q_dict['Queued Jobs'][qjob_id]['State']['HC State']['Cracked']
                                else:
                                    logger.debug('Still initializing')
                            else:
                                logger.error('No Queue')
                            #if 'Cracked' in q_dict['Queued Jobs'][qjob_id]['HC State']['HC State']:
                            #    del q_dict['Queued Jobs'][qjob_id]['HC State']['HC State']['Cracked']
                        except KeyError as err:
                            logger.debug('Cant clear cracked yet2: {}'.format(err))
            return q_dict, 200
        ###***apply validation here
        elif job_id == 'failed':
            return failed_dict, 200
        elif job_id == 'failedless':
            failess_dict = {}
            for job_id in failed_dict:
                if check_jobid(job_id):
                    failess_dict[job_id] = failed_dict[job_id]
            return failess_dict, 200
        ###***apply validation here
        ###***move to function in crackqueue
        elif job_id == 'complete':
            comp_dict = {}
            ###***add try/except here?
            for job_id in comp_list:
                comp_dict[job_id] = {}
                job = self.q.fetch_job(job_id)
                if job:
                    if job.meta and 'HC State' in job.meta.keys():
                        if isinstance(job.meta['HC State'], dict):
                            cracked = str(job.meta['HC State']['Cracked Hashes'])
                            total = str(job.meta['HC State']['Total Hashes'])
                            comp_dict[job_id]['Cracked'] = '{}/{}'.format(cracked, total)
                            comp_dict[job_id]['Running Time'] = job.meta['HC State']['Running Time']
                            ###***duplicated, make method?
                            try:
                                comp_dict[job_id]['Name'] = get_jobdetails(job.description)['name']
                            except KeyError:
                                comp_dict[job_id]['Name'] = 'No name'
                            except AttributeError:
                                comp_dict[job_id]['Name'] = 'No name'
                else:
                    logger.error('job.meta is missing for job: {}'.format(job_id))

            return comp_dict, 200
        elif job_id == 'completeless':
            comp_dict = {}
            ###***add try/except here?
            for job_id in comp_list:
                if check_jobid(job_id):
                    comp_dict[job_id] = {}
                    job = self.q.fetch_job(job_id)
                    if job:
                        if job.meta and 'HC State' in job.meta.keys():
                            if isinstance(job.meta['HC State'], dict):
                                cracked = str(job.meta['HC State']['Cracked Hashes'])
                                total = str(job.meta['HC State']['Total Hashes'])
                                comp_dict[job_id]['Cracked'] = '{}/{}'.format(cracked, total)
                                comp_dict[job_id]['Running Time'] = job.meta['HC State']['Running Time']
                                ###***duplicated, make method?
                                try:
                                    comp_dict[job_id]['Name'] = get_jobdetails(job.description)['name']
                                except KeyError:
                                    comp_dict[job_id]['Name'] = 'No name'
                                except AttributeError:
                                    comp_dict[job_id]['Name'] = 'No name'
                    else:
                        logger.error('job.meta is missing for job: {}'.format(job_id))

            return comp_dict, 200
        else:
            marsh_schema = parse_json_schema().load({'job_id': job_id})
            if len(marsh_schema.errors) > 0:
                logger.debug('Validation error: {}'.format(marsh_schema.errors))
                return marsh_schema.errors, 500
            else:
                job_id = marsh_schema.data['job_id'].hex
            check_job = check_jobid(job_id)
            if job_id in q_dict['Queued Jobs']:
                if not check_job:
                    ###***modify this to give better response?
                    return 401

                job = self.q.fetch_job(job_id)
                if job is not None:
                    job_details = get_jobdetails(job.description)
                    q_dict['Queued Jobs'][job_id]['Job Details'] = job_details
                    ###***add place in queue info
                    return q_dict['Queued Jobs'][job_id], 200
            elif job_id in q_dict['Current Job']:
                if not check_job:
                    ###***modify this to give better response?
                    return 401
                ###***add results
                ###***REFACTOR TO REMOVE USE OF FILE
                ###***validate file path here?
                ###***fix this up, why can't we pull the id from q_dict?
                job = self.q.fetch_job(job_id)
                if job is not None:
                    job_details = get_jobdetails(job.description)
                    job_dict = {
                        'Status': job.get_status(),
                        'Time started:': str(job.started_at),
                        'Time finished': str(job.ended_at),
                        'Job Details': job_details,
                        'Result': job.result,
                        'HC State': job.meta,
                        }
                    return job_dict, 200
                ###***dead code??
                result_file = '{}.json'.format(cur_list[0])
                with open(result_file, 'r') as status_json:
                    return (status_json.read(), q_dict['Current Job']), 200
                #with open('state.json', 'r') as status_json:
                #    return (status_json.read(), q_dict['Current Job']), 201
                    #@0return json.dumps(status_json.read()), 200
            elif job_id in comp_list:
                if not check_job:
                    ###***modify this to give better response?
                    return 401
                ###***VALIDATE??
                job = self.q.fetch_job(job_id)
                if job is not None:
                    job_details = get_jobdetails(job.description)
                    job_dict = {
                        'Status': job.get_status(),
                        'Time started:': str(job.started_at),
                        'Time finished': str(job.ended_at),
                        'Job Details': job_details,
                        'Result': job.result,
                        'HC State': job.meta,
                        }
                    return job_dict, 200
            elif job_id in failed_dict:
                if not check_job:
                    ###***modify this to give better response?
                    return 401
                job = self.q.fetch_job(job_id)
                if job is not None:
                    job_details = get_jobdetails(job.description)
                    job_dict = {
                        'Status': job.get_status(),
                        'Time started:': str(job.started_at),
                        'Time finished': str(job.ended_at),
                        'Job Details': job_details,
                        'Result': job.result,
                        'HC State': job.meta,
                        }
                ###***change http code?
                return job_dict, 200
            else:
                ###***update to handle 404 etc better
                return 'Not Found', 404

    @login_required
    def put(self, job_id):
        """
        Method to reorder the queue

        This will clear the queued jobs and re-add them in
        the order specified with a JSON batch add

        jobord_dict: dict
            Dictionary containing batch job add details as:
                {job_id: place}
            job_id: str hex representation of uuid job ID
            place: int indicating place in queue

        Returns
        ------
        """
        marsh_schema = parse_json_schema().load(request.json)
        if len(marsh_schema.errors) > 0:
            logger.debug('Validation error: {}'.format(marsh_schema.errors))
            return marsh_schema.errors, 500
        comp = rq.registry.FinishedJobRegistry('default',
                                               connection=self.redis_con)
        ###***change this to match reports, validate job_id correctly
        if job_id == "reorder":
            logger.debug('Reorder queue command received')
            logger.debug(marsh_schema.data['batch_job'])
            try:
                adder = Adder()
                for job in marsh_schema.data['batch_job']:
                    job_id = job['job_id']
                    if adder.session_check(self.log_dir, job_id):
                        logger.debug('Valid session found')
                        started = rq.registry.StartedJobRegistry('default',
                                                                 connection=self.redis_con)
                        cur_list = started.get_job_ids()
                        if job_id in cur_list:
                            logger.error('Job is already running')
                            return json.dumps({'msg': 'Job is already running'}), 500
                marsh_schema.data['batch_job'].sort(key=itemgetter('place'))
                for job in self.q.jobs:
                    job.set_status('finished')
                    job.save()
                    comp.add(job, -1)
                    job.cleanup(-1)
                Queue.dequeue_any(self.q, None, connection=self.redis_con)
                for job in marsh_schema.data['batch_job']:
                    #adder.post(job_id=job['job_id'])
                    #adder.post(job_id=job['job_id'])
                    j = self.q.fetch_job(job['job_id'])
                    ###***check this covers case when job is in requeued state
                    self.q.enqueue_job(j)
                    j.meta['CrackQ State'] = 'Run/Restored'
                    j.save_meta()

                return {'msg': 'Queue order updated'}, 200
            except Exception as err:
                ###***fix to specific exception types
                logger.error('Reorder failed: {}'.format(err))
                return 500

    @login_required
    def patch(self, job_id):
        """
        Method to stop/remove a job from the active queue to complete
        and cancel current hashcat job if it's already running

        Arguments
        ---------
        job_id: str
            hex reprisentation of uuid job ID

        Returns
        ------
        HTTP 204

        """
        marsh_schema = parse_json_schema().load({'job_id': job_id})
        if len(marsh_schema.errors) > 0:
            logger.debug('Validation error: {}'.format(marsh_schema.errors))
            return marsh_schema.errors, 500
        else:
            job_id = marsh_schema.data['job_id'].hex
        try:
            logger.info('Stopping job: {:s}'.format(job_id))
            job = self.q.fetch_job(job_id)

            started = rq.registry.StartedJobRegistry('default',
                                                     connection=self.redis_con)
            cur_list = started.get_job_ids()
            comp = rq.registry.FinishedJobRegistry('default',
                                                     connection=self.redis_con)
            if job_id in cur_list:
                job.meta['CrackQ State'] = 'Stop'
                job.save_meta()
                return 'Stopping Job: Sending signal to Hashcat', 204
            else:
                job.set_status('finished')
                job.save()
                comp.add(job, -1)
                job.cleanup(-1)
                ###***look into why lpop fails but dequeue_any works, but only against the chosen job
                #Queue.lpop([job_id], None, connection=self.redis_con)
                Queue.dequeue_any(self.q, None, connection=self.redis_con)
                return 'Stopped Job', 200
        except AttributeError as err:
            logger.error('Failed to stop job: {}'.format(err))
            return 'Invalid Job ID', 404

    @login_required
    def delete(self, job_id):
        """
        Method to remove a job from the queue completely
        and cancel current hashcat job if it's already running.
        This will remove all trace of the job

        Arguments
        ---------
        job_id: str
            hex reprisentation of uuid job ID

        Returns
        ------
        HTTP 204

        """
        marsh_schema = parse_json_schema().load({'job_id': job_id})
        if len(marsh_schema.errors) > 0:
            logger.debug('Validation error: {}'.format(marsh_schema.errors))
            return marsh_schema.errors, 500
        else:
            job_id = marsh_schema.data['job_id'].hex
        try:
            logger.info('Deleting job: {:s}'.format(job_id))
            job = self.q.fetch_job(job_id)

            started = rq.registry.StartedJobRegistry('default',
                                                     connection=self.redis_con)
            cur_list = started.get_job_ids()
            if job_id in cur_list:
                job.meta['CrackQ State'] = 'Stop'
                job.save_meta()
                ###***decrease this??
                time.sleep(6)
            job.delete()
            started.cleanup()
            ###***re-add this when delete job bug is fixed
            #del_jobid(job_id)
            return 'Deleting Job', 204
        except AttributeError as err:
            logger.error('Failed to delete job: {}'.format(err))
            return 'Invalid Job ID', 404


class Options(Resource):
    """
    Class for pulling option information, such as a list of available
    rules and wordlists

    """
    def __init__(self):
        self.crack_q = crackqueue.Queuer()
        self.q = self.crack_q.q_connect()
        self.crack = run_hashcat.Crack()
        rconf = CRACK_CONF['redis']
        self.redis_con = Redis(rconf['host'], rconf['port'])

    @login_required
    def get(self):
        """
        Method to get config information 


        Returns
        ------
        hc_dict: dictionary 
            crackq config options for rules/wordlists


        """
        hc_rules = [rule for rule in CRACK_CONF['rules']]
        hc_words = [word for word in CRACK_CONF['wordlists']]
        hc_maskfiles = [maskfile for maskfile in CRACK_CONF['masks']]
        hc_modes = dict(hash_modes.HModes.modes_dict())
        hc_att_modes = {
                        '0': 'Straight',
                        '1': 'Combination',
                        '3': 'Brute-Force',
                        '6': 'Hybrid Wordlist + Mask',
                        '7': 'Hybrid Mask + Wordlist',
                    }
        hc_dict = {
                    'Rules': hc_rules,
                    'Wordlists': hc_words,
                    'Mask Files': hc_maskfiles,
                    'Hash Modes': hc_modes,
                    'Attack Modes': hc_att_modes,
                }
        return hc_dict, 200


class Adder(Resource):
    """
    Separate class for adding jobs

    """
    def __init__(self):
        self.crack_q = crackqueue.Queuer()
        self.q = self.crack_q.q_connect()
        self.crack = run_hashcat.Crack()
        self.log_dir = CRACK_CONF['files']['log_dir']
        rconf = CRACK_CONF['redis']
        self.redis_con = Redis(rconf['host'], rconf['port'])

    def mode_check(self, mode):
        """
        Mode to check supplied hash mode is supported by Hashcat

        Arguments
        ---------
        mode: int
            hashcat mode number to check

        Returns
        -------
        mode: int/boolean
            returns mode if found else false

        """
        modes_dict = dict(hash_modes.HModes.modes_dict())
        logger.debug('Checking hash mode is supported: {}'.format(mode))
        if str(mode) in modes_dict.keys():
            return int(mode)
        else:
            return False

    def get_restore(self, log_dir, job_id):
        """
        Get restore number from CrackQ json status file
        Arguments
        ---------
        log_dir: str
            log directory
        job_id: str
            job ID string
        Returns
        -------
        restore: int
            Restore number to be used with hashcat skip
            returns 0 on error
        """
        logger.info('Checking for restore value')
        if job_id.isalnum():
            job_file = Path(log_dir).joinpath('{}.json'.format(job_id))
            logger.debug('Using session file: {}'.format(job_file))
            try:
                with open(job_file) as fh_job_file:
                    try:
                        status_json = json.loads(fh_job_file.read())
                        logger.debug('Restoring job details: {}'.format(status_json))
                        #restore = status_json['Restore Point']
                        return status_json
                    except IOError as err:
                        logger.warning('Invalid job ID: {}'.format(err))
                        return 0

                    except TypeError as err:
                        logger.warning('Invalid job ID: {}'.format(err))
                        return 0
            except IOError as err:
                logger.warning('Restore file Error: {}'.format(err))
                return 0
            except json.decoder.JSONDecodeError as err:
                logger.warning('Restore file Error: {}'.format(err))
                return 0
        else:
            logger.warning('Invalid job ID')
            return 0

    def session_check(self, log_dir, job_id):
        """
        Check for existing session and  return the ID if present
        else False

        Arguments
        ---------
        log_dir: str
            directory containing cracker log and session files
        job_id: str
            job/session id string (alnum)
        Returns
        -------
        sess_id: bool
            True if session/job ID is valid and present
        """
        ###*** add checking for restore value
        logger.info('Checking for existing session')
        log_dir = Path(log_dir)
        sess_id = False
        if job_id.isalnum():
            try:
                #files = [f for f in Path.iterdir(log_dir)]
                for f in Path.iterdir(log_dir):
                    if job_id in str(f):
                        sess_id = True
                        break
            except ValueError as err:
                logger.debug('Invalid session ID: {}'.format(err))
                sess_id = False
            except Exception as err:
                ###***fix/remove?
                logger.warning('Invalid session: {}'.format(err))
                sess_id = False
        else:
            logger.debug('Invalid session ID provided')
            sess_id = False
        if sess_id is not False:
            logger.info('Existing session found')
        return sess_id

    @login_required
    def post(self, job_id=None):
        """
        Method to post a new job to the queue

        job_id: str
            hex representation of uuid job ID

        Returns
        ------
        boolean
            True/False success failure
        HTTP_status: int
            HTTP status, 201  or 500

        """
        marsh_schema = parse_json_schema().load(request.json)
        if len(marsh_schema.errors) > 0:
            logger.debug('Validation error: {}'.format(marsh_schema.errors))
            return marsh_schema.errors, 500
        else:
            args = marsh_schema.data
        try:
            job_id = args['job_id'].hex
        except KeyError as err:
            logger.debug('No job ID provided')
            job_id = None
        except AttributeError as err:
            logger.debug('No job ID provided')
            job_id = None
        # Check for existing session info
        if job_id:
            if job_id.isalnum():
                if self.session_check(self.log_dir, job_id):
                    logger.debug('Valid session found')
                    started = rq.registry.StartedJobRegistry('default',
                                                             connection=self.redis_con)
                    cur_list = started.get_job_ids()
                    if job_id in cur_list:
                        logger.error('Job is already running')
                        return json.dumps({'msg': 'Job is already running'}), 500
                    ###***SET THIS TO CHECK MATCHES IN A DICT RATHER THAN DIRECT
                    ###***REVIEW ALL CONCATINATION
                    ###***taking input here, review
                    outfile = '{}{}.cracked'.format(self.log_dir, job_id)
                    hash_file = '{}{}.hashes'.format(self.log_dir, job_id)
                    pot_path = '{}crackq.pot'.format(self.log_dir)
                    job_deets = self.get_restore(self.log_dir, job_id)
                    job = self.q.fetch_job(job_id)
                    if job_deets == 0:
                        logger.debug('Job not previously started, restore = 0')
                        self.q.enqueue_job(job)
                        return json.dumps({'msg': 'Invalid Job ID'}), 202
                        #return json.dumps({'msg': 'Job restore error.'}), 500
                    hc_args = {
                        'crack': self.crack,
                        'hash_file': hash_file,
                        'session': job_id,
                        'wordlist': job_deets['wordlist'] if 'wordlist' in job_deets else None,
                        'mask': job_deets['mask'] if 'mask' in job_deets else None,
                        'mask_file': True if job_deets['mask'] in CRACK_CONF['masks'] else False,
                        'attack_mode': int(job_deets['attack_mode']),
                        'hash_mode': int(job_deets['hash_mode']),
                        'outfile': outfile,
                        'rules': job_deets['rules'] if 'rules' in job_deets else None,
                        'restore': job_deets['restore'],
                        'username': job_deets['username'] if 'user' in job_deets else None,
                        'brain': False if 'disable_brain' in job_deets else True,
                        'name': job_deets['name'] if 'name' in job_deets else None,
                        'pot_path': pot_path,
                        }
                    job = self.q.fetch_job(job_id)
                    #job.result = 'Run/Restored' #Restored/Run'
                    #job.result = None
                    job.meta['CrackQ State'] = 'Run/Restored'
                    job.save_meta()
                else:
                    return json.dumps({'msg': 'Invalid Job ID'}), 500
            else:
                return json.dumps({'msg': 'Invalid Job ID'}), 500
        else:
            logger.debug('Creating new session')
            job_id = uuid.uuid4().hex
            add_jobid(job_id)
            ###***SET THIS TO CHECK MATCHES IN A DICT RATHER THAN DIRECT
            ###***REVIEW ALL CONCATINATION
            ###***taking input here, review
            ###***use pathlib validation?
            outfile = '{}{}.cracked'.format(self.log_dir, job_id)
            hash_file = '{}{}.hashes'.format(self.log_dir, job_id)
            pot_path = '{}crackq.pot'.format(self.log_dir)
            ###***do attack mode check too
            try:
                attack_mode = int(args['attack_mode'])
            except TypeError:
                attack_mode = None
            try:
                logger.debug('Writing hashes to file: {}'.format(hash_file))
                with open(hash_file, 'w') as hash_fh:
                    for hash_l in args['hash_list']:
                        ###***REVIEW THIS
                        hash_fh.write(hash_l.rstrip() + '\n')
            except KeyError as err:
                logger.debug('No hash list provided: {}'.format(err))
                return json.dumps({'msg': 'No hashes provided'}), 500
            check_m = self.mode_check(args['hash_mode'])
            logger.debug('Hash mode check: {}'.format(check_m))
            ###***change to if check_m
            if check_m is not False:
                try:
                    mode = int(check_m)
                except TypeError as err:
                    logger.error('Incorrect type supplied for hash_mode:'
                                 '\n{}'.format(err))
                    return json.dumps({'msg': 'Invalid hash mode selected'}), 500
            else:
                return json.dumps({'msg': 'Invalid hash mode selected'}), 500
            ###***add checks??
            if attack_mode != 3:
                if args['wordlist'] in CRACK_CONF['wordlists']:
                    wordlist = CRACK_CONF['wordlists'][args['wordlist']]
                else:
                    return json.dumps({'msg': 'Invalid wordlist selected'}), 500
            try:
                mask = args['mask']
            except KeyError as err:
                logger.debug('Mask value not provided')
                mask = False
            ###***review this
            try:
                logger.debug('Checking mask_file valid: {}'.format(args['mask_file']))
                if args['mask_file'] is None:
                    mask_file = None
                elif isinstance(args['mask_file'], list):
                    mask_file = []
                    for mask in args['mask_file']:
                        if mask in CRACK_CONF['masks']:
                            #mask_name = CRACK_CONF['masks'][mask]
                            logger.debug('Using mask file: {}'.format(mask))
                            mask_file.append(CRACK_CONF['masks'][mask])
                else:
                    return json.dumps({'msg': 'Invalid mask file selected'}), 500
            except KeyError as err:
                rules = None
                logger.debug('No mask file provided: {}'.format(err))
            # this is just set to use the first mask file in the list for now
            mask = mask_file[0] if mask_file else mask
            ###***review this
            try:
                logger.debug('Checking rules valid: {}'.format(args['rules']))
                if args['rules'] is None:
                    rules = None
                elif isinstance(args['rules'], list):
                    rules = []
                    for rule in args['rules']:
                        if rule in CRACK_CONF['rules']:
                            logger.debug('Using rules file: {}'.format(CRACK_CONF['rules'][rule]))
                            rules.append(CRACK_CONF['rules'][rule])
                    #rules = [rule for rule in CRACK_CONF['rules'][args['rules']]]
                else:
                    return json.dumps({'msg': 'Invalid rules selected'}), 500
            except KeyError as err:
                rules = None
                logger.debug('No rules provided: {}'.format(err))
            try:
                username = args['username']
            except KeyError as err:
                logger.debug('Username value not provided')
                username = False
            try:
                logger.debug(args)
                if args['disable_brain']:
                    logger.debug('Brain disabled')
                    brain = False
                else:
                    brain = True
            except KeyError as err:
                logger.debug('Brain not disabled: {}'.format(err))
                brain = True
            try:
                name = args['name']
            except KeyError as err:
                logger.debug('Name value not provided')
                name = None
            """    
            try:
                marsh_schema = parse_json_schema().load({'name': job_id})
                if len(marsh_schema.errors) > 0:
                    logger.debug('Validation error: {}'.format(marsh_schema.errors))
                    return marsh_schema.errors, 500
                else:
                    ###***check this
                    #name = marsh_schema.data['name']
                    name = args['name']
            except KeyError as err:
                logger.debug('Name value not provided')
                name = None
            """
            hc_args = {
                'crack': self.crack,
                'hash_file': hash_file,
                'session': job_id,
                'wordlist': wordlist if attack_mode != 3 else None,
                'mask': mask if attack_mode > 2 else None,
                'mask_file': True if mask_file else False,
                'attack_mode': attack_mode,
                'hash_mode': mode,
                'outfile': outfile,
                'rules': rules,
                #'#restore': restore if restore else None,
                'username': username,
                'brain': brain,
                'name': name,
                'pot_path': pot_path,
                    }
        q_args = {
                'func': self.crack.hc_worker,
                'job_id': job_id,
                'kwargs': hc_args}
        try:
            q = self.crack_q.q_connect()
            self.crack_q.q_add(q, q_args)
            logger.info('API Job {} added to queue'.format(job_id))
            logger.debug('Job Details: {}'.format(q_args))
            job = self.q.fetch_job(job_id)
            job.meta['CrackQ State'] = 'Run'
            job.meta['Speed Array'] = []
            job.save_meta()
            return job_id, 202
        ###***make this more specific?
        except Exception as err:
            logger.info('API post failed:\n{}'.format(err))
            return job_id, 500


def reporter(cracked_path, report_path):
    """
    Simple method to call pypal and save report (html & json)
    """
    nltk.download('wordnet')
    report = pypal.Report(cracked_path=cracked_path,
                          lang='EN',
                          lists='/opt/crackq/build/pypal/src/lists/')
    report_json = report.report_gen()
    with open(report_path, 'w') as fh_report:
        fh_report.write(json.dumps(report_json))
    return True

###***remove later if not used
def output_html(data, code, headers=None):
    """
    This function allows flask-restful to return HTML
    """
    resp = make_response(data, code)
    resp.headers.extend(headers or {})
    return resp



class Reports(Resource):
    """
    Class for creating and serving HTML password analysis reports

    Calls pypal with the location of the specified crackq output
    file for a given job_id, provided auth is accepted
    """
    def __init__(self):
        self.crack_q = crackqueue.Queuer()
        self.q = self.crack_q.q_connect()
        self.report_q = self.crack_q.q_connect(queue='reports')
        rconf = CRACK_CONF['redis']
        self.redis_con = Redis(rconf['host'], rconf['port'])
        self.report_dir = CRACK_CONF['reports']['dir']
        self.log_dir = CRACK_CONF['files']['log_dir']
        self.adder = Adder()
        #self.representation = 'text/html'

    #@api.representation('text/html')
    @login_required
    def get(self, job_id=None):
        """
        Method to get report file

        Returns
        ------
        report: file
            HTML report file generated by Pypal
        """
        marsh_schema = parse_json_schema().load(request.args)
        if len(marsh_schema.errors) > 0:
            logger.debug('Validation error: {}'.format(marsh_schema.errors))
            return marsh_schema.errors, 500
        else:
            args = marsh_schema.data
        if 'job_id' not in args:
            logger.debug('Reports queue requested')
            failed = rq.registry.FailedJobRegistry('reports',
                                                   connection=self.redis_con)
            comp = rq.registry.FinishedJobRegistry('reports',
                                                   connection=self.redis_con)
            started = rq.registry.StartedJobRegistry('reports',
                                                    connection=self.redis_con)
            reports_dict = {}
            reports_dict.update({j: 'Generated' for j in comp.get_job_ids()})
            reports_dict.update({j: 'Failed' for j in failed.get_job_ids()})
            reports_dict.update({j: 'Running' for j in started.get_job_ids()})
            return reports_dict, 200
        else:
            job_id = str(args['job_id'].hex)
        # Check for existing session info
        logger.debug('User requesting report')
        if job_id:
            if job_id.isalnum():
                check_job = check_jobid(job_id)
                if not check_job:
                    return 401
                if self.adder.session_check(self.log_dir, job_id):
                    logger.debug('Valid session found')
                    ###***REVIEW ALL CONCATINATION
                    ###***taking input here, review
                    #outfile = '{}{}.cracked'.format(self.log_dir, job_id)
                    #report_file = '{}_report.html'.format(self.log_dir, job_id)
                    #job_deets = self.get_restore(self.log_dir, job_id)
                    #job = self.q.fetch_job(job_id)
                    report = '{}_report.html'.format(job_id)
                    report_path = Path('{}{}.json'.format(self.report_dir,
                                                              job_id))
                    #crackq.app.static_folder = str(self.report_dir)
                    #json_report = self.report_dir.joinpath('{}_report.json'.format(job_id))
                    try:
                        with report_path.open('r') as rep:
                            return json.loads(rep.read()), 200
                    except IOError as err:
                        logger.debug('Error reading report: {}'.format(err))
                        return json.dumps({'msg': 'No report generated for'
                                                  'this job'}), 500
        else:
            return json.dumps({'msg': 'Invalid Job ID'}), 404

    @login_required
    def post(self):
        """
        Method to trigger report generation
        """
        ###***make this a decorator??
        logger.debug('User requesting report')
        marsh_schema = parse_json_schema().load(request.json)
        if len(marsh_schema.errors) > 0:
            logger.debug('Validation error: {}'.format(marsh_schema.errors))
            return marsh_schema.errors, 500
        else:
            args = marsh_schema.data
        try:
            job_id = args['job_id'].hex
        except KeyError as err:
            logger.debug('No job ID provided')
            job_id = None
        except AttributeError as err:
            logger.debug('No job ID provided')
            job_id = None
        except TypeError as err:
            logger.debug('No job ID provided')
            job_id = None
        # Check for existing session info
        if job_id:
            self.adder = Adder()
            if job_id.isalnum():
                check_job = check_jobid(job_id)
                if not check_job:
                    return {'msg': 'Not Authorized'}, 401
                if self.adder.session_check(self.log_dir, job_id):
                    logger.debug('Valid session found')
                    ###***REVIEW ALL CONCATINATION
                    ###***taking input here, review
                    cracked_path = Path('{}{}.cracked'.format(self.log_dir,
                                                              job_id))
                    report_path = Path('{}{}.json'.format(self.report_dir,
                                                              job_id))
                    #hash_file = '{}{}.hashes'.format(self.log_dir, job_id)
                    #job_deets = self.get_restore(self.log_dir, job_id)
                    job = self.q.fetch_job(job_id)
                    if job.meta['HC State']['Cracked Hashes'] < 100:
                        return json.dumps({'msg': 'Cracked password list too '
                                                  'small for meaningful '
                                                  'analysis'}), 500
                    try:
                        logger.debug('Generating report: {}'
                                     .format(cracked_path))
                        rep = self.report_q.enqueue(reporter,
                                                    cracked_path,
                                                    report_path,
                                                    job_timeout=10080,
                                                    result_ttl=604800,
                                                    job_id='{}_report'.format(job_id), ttl=-1)
                        if rep:
                            return json.dumps({'msg': 'Successfully queued '
                                               'report generation'}), 202
                        else:
                            return json.dumps({'msg': 'Error no report data '
                                               'returned'}), 500
                    except IOError as err:
                        logger.debug('No cracked passwords found for this job')
                        return json.dumps({'msg': 'No report available for Job ID'}), 404
        else:
            return json.dumps({'msg': 'Invalid Job ID'}), 404


#app = Flask(__name__)
#app = crackq.app
#api = Api(app)
#api.add_resource(Options, '/options')
#api.add_resource(Queuing, '/queuing/<job_id>')
#api.add_resource(Adder, '/add')
#app = create_app()

#if __name__ == '__main__':
#    app.run(host='0.0.0.0', port=8080, debug=True)
