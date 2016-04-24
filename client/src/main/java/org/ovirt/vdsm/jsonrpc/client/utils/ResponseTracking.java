package org.ovirt.vdsm.jsonrpc.client.utils;

import org.ovirt.vdsm.jsonrpc.client.JsonRpcRequest;
import org.ovirt.vdsm.jsonrpc.client.internal.JsonRpcCall;
import org.ovirt.vdsm.jsonrpc.client.reactors.ReactorClient;
import org.ovirt.vdsm.jsonrpc.client.utils.retry.RetryContext;

public class ResponseTracking {

    private JsonRpcRequest request;
    private long timeout;
    private JsonRpcCall call;
    private RetryContext context;
    private ReactorClient client;
    private boolean resetConnection;

    public ResponseTracking(JsonRpcRequest request, JsonRpcCall call, RetryContext context, long timeout, ReactorClient client, boolean resetConnection) {
        this.request = request;
        this.timeout = timeout;
        this.call = call;
        this.context = context;
        this.client = client;
        this.resetConnection = resetConnection;
    }

    public JsonRpcRequest getRequest() {
        return this.request;
    }

    public long getTimeout() {
        return this.timeout;
    }

    public void setTimeout(long timeout) {
        this.timeout = timeout;
    }

    public JsonRpcCall getCall() {
        return this.call;
    }

    public RetryContext getContext() {
        return this.context;
    }

    public ReactorClient getClient() {
        return client;
    }

    public boolean isResetConnection() {
        return resetConnection;
    }

    public void setResetConnection(boolean resetConnection) {
        this.resetConnection = resetConnection;
    }
}
