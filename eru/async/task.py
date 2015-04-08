#!/usr/bin/python
#coding:utf-8

import logging
from celery import current_app

from eru.common import code
from eru.common.clients import rds
from eru.async import dockerjob
from eru.utils.notify import TaskNotifier
from eru.models import Container, Task, Core, Port, Version


logger = logging.getLogger(__name__)


def add_container_backends(container):
    """单个container所拥有的后端服务
    HKEYS app_key 可以知道有哪些后端
    HGET 上面的结果可以知道后端都从哪里拿
    SMEMBERS entrypoint_key 可以拿出所有的后端
    """
    app_key = 'eru:app:{0}:backends'.format(container.appname)
    entrypoint_key = 'eru:app:{0}:entrypoint:{1}:backends'.format(container.appname, container.entrypoint)
    rds.hset(app_key, container.entrypoint, entrypoint_key)

    backends = ['%s:%s' % (container.host.ip, p.port) for p in container.ports]
    if backends:
        rds.sadd(entrypoint_key, *backends)

def remove_container_backends(container):
    """删除单个container的后端服务
    并不删除有哪些entrypoint, 这些service discovery方便知道哪些没了"""
    entrypoint_key = 'eru:app:{0}:entrypoint:{1}:backends'.format(container.appname, container.entrypoint)
    backends = ['%s:%s' % (container.host.ip, p.port) for p in container.ports]
    if backends:
        rds.srem(entrypoint_key, *backends)

def add_container_for_agent(container):
    """agent需要从key里取值出来去跟踪
    SMEMBERS key 可以拿出这个host上所有的container"""
    host = container.host
    key = 'eru:agent:{0}:containers'.format(host.name)
    rds.sadd(key, container.container_id)

def remove_container_for_agent(container):
    host = container.host
    key = 'eru:agent:{0}:containers'.format(host.name)
    rds.srem(key, container.container_id)

def publish_to_service_discovery(*appnames):
    for appname in appnames:
        rds.publish('eru:discovery:published', appname)

def dont_report_these(container_ids):
    """告诉agent这些不要care了"""
    flags = {'eru:agent:%s:container:flag' % cid: 1 for cid in container_ids}
    rds.mset(**flags)

@current_app.task()
def create_docker_container(task_id, ncontainer, core_ids, port_ids):
    """
    这个任务是在 host 上部署 ncontainer 个容器.
    可能占用 cores 这些核, 以及 ports 这些端口.
    """
    task = Task.get(task_id)
    notifier = TaskNotifier(task)
    cores = Core.get_multi(core_ids)
    ports = Port.get_multi(port_ids)
    cids = []
    try:
        host = task.host
        version = task.version
        entrypoint = task.props['entrypoint']
        env = task.props['env']
        containers = dockerjob.create_containers(
            host, version, entrypoint,
            env, ncontainer, cores, ports
        )
    except Exception, e:
        logger.exception(e)
        host.release_cores(cores)
        host.release_ports(ports)
        task.finish_with_result(code.TASK_FAILED, container_ids=cids)
        notifier.pub_fail()
    else:
        for cid, cname, entrypoint, used_cores, expose_ports in containers:
            c = Container.create(cid, host, version, cname, entrypoint, used_cores, env, expose_ports)
            if not c:
                continue
            notifier.notify_agent(cid)
            add_container_for_agent(c)
            add_container_backends(c)
            cids.append(cid)
        publish_to_service_discovery(version.name)
        task.finish_with_result(code.TASK_SUCCESS, container_ids=cids)
        notifier.pub_success()


@current_app.task()
def build_docker_image(task_id, base):
    task = Task.get(task_id)
    notifier = TaskNotifier(task)
    try:
        repo, tag = base.split(':', 1)
        notifier.store_and_broadcast(dockerjob.pull_image(task.host, repo, tag))
        notifier.store_and_broadcast(dockerjob.build_image(task.host, task.version, base))
        notifier.store_and_broadcast(dockerjob.push_image(task.host, task.version))
        try:
            dockerjob.remove_image(task.version, task.host)
        except:
            pass
    except Exception, e:
        logger.exception(e)
        task.finish_with_result(code.TASK_FAILED)
        notifier.pub_fail()
    else:
        task.finish_with_result(code.TASK_SUCCESS)
        notifier.pub_success()
    finally:
        notifier.pub_build_finish()


@current_app.task()
def remove_containers(task_id, cids, rmi):
    task = Task.get(task_id)
    notifier = TaskNotifier(task)
    containers = Container.get_multi(cids)
    container_ids = [c.container_id for c in containers]
    host = task.host
    try:
        flags = {'eru:agent:%s:container:flag' % cid: 1 for cid in container_ids}
        rds.mset(**flags)
        for c in containers:
            remove_container_backends(c)
        appnames = {c.appname for c in containers}
        publish_to_service_discovery(*appnames)

        dockerjob.remove_host_containers(containers, host)
        if rmi:
            dockerjob.remove_image(task.version, host)
    except Exception, e:
        logger.exception(e)
        task.finish_with_result(code.TASK_FAILED)
        notifier.pub_fail()
    else:
        for c in containers:
            c.delete()
        task.finish_with_result(code.TASK_SUCCESS)
        notifier.pub_success()
        if container_ids:
            rds.srem('eru:agent:%s:containers' % host.name, *container_ids)
        rds.delete(*flags.keys())


@current_app.task()
def update_containers(task_id, version_id, cids):
    task = Task.get(task_id)
    notifier = TaskNotifier(task)
    version = Version.get(version_id)
    containers = Container.get_multi(cids)
    port_count = sum(len(c.ports.all()) for c in containers)
    container_ids = [c.container_id for c in containers]
    host = task.host
    rs = []
    # cids 要从 eru:agent:%s:containers host.name 里删掉
    # eru:agent:%s:container:flag ~ cids 要mset
    # backends 要删掉
    backends = {}

    u_containers = [c for c in containers if c.version_id != version_id]
    if not u_containers:
        return
    used_ports = host.get_free_ports(port_count)
    host.occupy_ports(used_ports)
    try:

        for c in u_containers:
            _backends = ['%s:%s' % (host.ip, p.port) for p in c.ports]
            backends.setdefault('eru:app:%s:entrypoint:%s:backends' % (c.appname, c.entrypoint), []).append(_backends)
        rs = dockerjob.update_containers(host, u_containers, version, used_ports)
    except Exception, e:
        logger.exception(e)
        host.release_ports(used_ports)
        task.finish_with_result(code.TASK_FAILED)
        notifier.pub_fail()
    else:
        if not rs:
            return
        for c, (cid, cname, expose_ports) in zip(u_containers, rs):
            c.transform(version, expose_ports, cid, cname)
        flags = {'eru:agent:%s:container:flag' % cid: 1 for cid in container_ids}
        rds.mset(**flags)
        dockerjob.remove_container_by_cid(container_ids, host)
        rds.delete(*flags.keys())

        task.finish_with_result(code.TASK_SUCCESS)

        for c in u_containers:
            add_container_backends(c)
            add_container_for_agent(c)
        
        for key, bs in backends.iteritems():
            if bs:
                rds.srem(key, *bs)
        rds.srem('eru:agent:%s:containers' % host.name, *container_ids)
        publish_to_service_discovery(version.name)
        notifier.pub_success()
